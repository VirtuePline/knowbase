"""从问答入口验证 LangGraph 实际执行的节点和权限边界。"""
import unittest
from dataclasses import replace
from unittest.mock import Mock, PropertyMock, patch

from langchain_core.callbacks import BaseCallbackHandler

from app.core.answer import context, service
from app.core.retrieval.bm25 import BM25Index
from app.domain.schemas import Chunk
from app.services import access, database
from app.services import retrieval_service


QUESTION = "员工年假制度"
VISIBLE_TEXT = "员工年假制度：员工每年可休五天。"
PRIVATE_TEXT = "员工年假制度：财务部门内部备注。"


class NodeTrace(BaseCallbackHandler):
    def __init__(self):
        self.nodes = []

    def on_chain_start(self, serialized, inputs, *, metadata=None, **kwargs):
        node = (metadata or {}).get("langgraph_node")
        if node and node not in self.nodes:
            self.nodes.append(node)


class RagWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.allowed = {"hr"}
        documents = Mock()
        documents.find.side_effect = lambda *args, **kwargs: [
            {"_id": doc_id} for doc_id in self.allowed
        ]
        index = BM25Index().build([
            Chunk("private-chunk", "finance", "财务制度.md", "md", "第1节", PRIVATE_TEXT),
            Chunk("public-chunk", "hr", "人事制度.md", "md", "第1节", VISIBLE_TEXT),
        ])
        self.enterContext(access.identity_scope({
            "username": "hr-user", "role": "user", "department_ids": ["hr"],
            "department_roles": {"hr": "editor"},
        }))
        self.enterContext(patch.object(database, "_docs", return_value=documents))
        self.enterContext(patch.object(retrieval_service.RetrievalService, "index",
                                       new_callable=PropertyMock, return_value=index))
        self.enterContext(patch.object(retrieval_service, "get_embedder", return_value=None))
        self.reranker = self.enterContext(patch.object(retrieval_service, "get_reranker"))
        self.reranker.return_value.score.return_value = [0.9]
        self.enterContext(patch.object(service, "answer_penalty_counts", return_value={}))
        self.enterContext(patch.object(service, "uploaded_dates", return_value={}))
        self.enterContext(patch.object(service.network_policy, "external_apis_enabled", return_value=True))
        self.enterContext(patch.object(service, "settings", replace(service.settings, llm_api_key="test")))
        self.complete = self.enterContext(patch.object(service, "llm_complete", return_value="每年五天。"))

        self.trace = NodeTrace()
        graph = service._ANSWER_GRAPH
        invoke = graph.invoke

        def traced_invoke(state, config=None, **kwargs):
            config = {**(config or {}), "callbacks": [self.trace]}
            return invoke(state, config=config, **kwargs)

        self.enterContext(patch.object(graph, "invoke", side_effect=traced_invoke))

    def test_request_retrieves_authorized_evidence_then_generates(self):
        response = service.answer(QUESTION, fast_mode=False, llm_polish=True)

        self.assertEqual(self.trace.nodes, [
            "prepare", "retrieve", "assess_evidence", "build_context", "generate", "finalize",
        ])
        self.assertTrue(response.llm_used)
        self.assertEqual([source.doc_id for source in response.sources], ["hr"])
        self.assertEqual(self.reranker.return_value.score.call_args.args[1], [VISIBLE_TEXT])
        context = self.complete.call_args.args[1]
        self.assertIn(VISIBLE_TEXT, context)
        self.assertNotIn(PRIVATE_TEXT, context)

    def test_fast_path_retrieves_but_does_not_rerank_or_generate(self):
        response = service.answer(QUESTION, fast_mode=True, llm_polish=True)

        self.assertEqual(self.trace.nodes, ["prepare", "retrieve", "assess_evidence", "fallback", "finalize"])
        self.assertEqual([source.doc_id for source in response.sources], ["hr"])
        self.reranker.assert_not_called()
        self.complete.assert_not_called()

    def test_model_sources_follow_the_context_budget(self):
        self.allowed.add("finance")
        self.reranker.return_value.score.return_value = [0.9, 0.8]
        with patch.object(context, "settings", replace(context.settings, max_context_chars=48)):
            response = service.answer(QUESTION, fast_mode=False, llm_polish=True)

        self.assertTrue(response.llm_used)
        self.assertEqual(len(response.sources), 1)
        prompt = self.complete.call_args.args[1]
        self.assertIn(response.sources[0].doc_name, prompt)
        self.assertNotIn("【记录 2】", prompt)

    def test_empty_model_context_returns_evidence_without_calling_model(self):
        with patch.object(context, "settings", replace(context.settings, max_context_chars=5)):
            response = service.answer(QUESTION, fast_mode=False, llm_polish=True)

        self.assertIn(VISIBLE_TEXT, response.answer)
        self.assertFalse(response.llm_used)
        self.complete.assert_not_called()

    def test_no_answer_from_model_routes_back_to_original_evidence(self):
        self.complete.return_value = "NO_ANSWER"
        response = service.answer(QUESTION, fast_mode=False, llm_polish=True)

        self.assertEqual(self.trace.nodes, [
            "prepare", "retrieve", "assess_evidence", "build_context", "generate", "fallback", "finalize",
        ])
        self.assertFalse(response.llm_used)
        self.assertIn(VISIBLE_TEXT, response.answer)

    def test_permission_revocation_in_generation_discards_answer(self):
        def revoke(*args):
            self.allowed.clear()
            return "撤权后的旧答案"

        self.complete.side_effect = revoke
        response = service.answer(QUESTION, fast_mode=False, llm_polish=True)

        self.assertEqual(self.trace.nodes[-2:], ["generate", "finalize"])
        self.assertEqual(response.status, "not_found")
        self.assertEqual(response.sources, [])
        self.assertIn("权限已变更", response.answer)
        self.assertNotIn("撤权后的旧答案", response.answer)

    def test_empty_authorized_scope_uses_fallback_without_generation(self):
        self.allowed.clear()
        response = service.answer(QUESTION, fast_mode=False, llm_polish=True)

        self.assertEqual(self.trace.nodes, ["prepare", "retrieve", "assess_evidence", "fallback", "finalize"])
        self.assertEqual(response.answer_origin, "none")
        self.assertEqual(response.sources, [])
        self.complete.assert_not_called()
