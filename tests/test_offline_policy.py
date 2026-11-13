"""离线开关的出站封锁与本地降级；禁止真实网络，不加载或下载模型。"""
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from app.config import settings
from app.core import network_policy
from app.core.answer import service as answers
from app.core.ingest import analyzer
from app.core.llm import client as llm
from app.core.retrieval import embeddings
from app.core.retrieval.bm25 import BM25Index
from app.core.search import client as search
from app.domain.schemas import Block, Chunk, ScoredChunk
from app.services import database, retrieval_service, weekly


class OfflinePolicyTest(unittest.TestCase):
    def setUp(self):
        policy = patch.object(network_policy, "external_apis_enabled", return_value=False)
        policy.start()
        self.addCleanup(policy.stop)
        opening = patch.object(llm.urllib.request, "urlopen", side_effect=AssertionError("unexpected HTTP"))
        self.opening = opening.start()
        self.addCleanup(opening.stop)

    def test_all_llm_entry_points_and_vision_are_blocked_before_http(self):
        for call in (
            lambda: llm._post("https://example.com/chat", {}, {}),
            lambda: llm.chat("system", "question"),
            lambda: llm.complete("question", "private document"),
            lambda: llm.rewrite_query("question", [{"role": "user", "content": "earlier"}]),
            lambda: llm.draft_solution("question", "private document"),
            lambda: llm.describe_image(b"synthetic-image", "image/png"),
        ):
            with self.subTest(call=call), self.assertRaises(llm.LLMError) as error:
                call()
            self.assertIn("离线", str(error.exception))
        self.opening.assert_not_called()

    def test_search_is_blocked_even_with_provider_and_key_configured(self):
        config = replace(settings, web_search_enabled=True, web_search_api_key="test-key")
        with patch.object(search, "settings", config):
            self.assertEqual(search.search("报销流程"), [])
        self.opening.assert_not_called()

    def test_cached_api_embedding_cannot_bypass_a_later_disable(self):
        cached = embeddings.ApiEmbedder()
        config = replace(settings, embedding_enabled=True, embedding_provider="api", embedding_api_key="test-key")
        with patch.object(embeddings, "settings", config), patch.object(embeddings, "_embedder", cached):
            self.assertIsNone(embeddings.get_embedder())
            with self.assertRaises(embeddings.EmbeddingError):
                cached.query("企业资料")
            with self.assertRaises(embeddings.EmbeddingError):
                cached.embed(["新文档"])
        self.opening.assert_not_called()

    def test_a_disable_after_request_preparation_still_prevents_http(self):
        with patch.object(network_policy, "external_apis_enabled", side_effect=[True, False]):
            with self.assertRaises(llm.LLMError):
                llm._post("https://example.com/chat", {}, {})
        self.opening.assert_not_called()

    def test_cold_local_embedding_uses_only_cached_files_and_does_not_download(self):
        factory = MagicMock(side_effect=FileNotFoundError("no local model"))
        with patch.dict(sys.modules, {"fastembed": SimpleNamespace(TextEmbedding=factory)}):
            with self.assertRaises(embeddings.EmbeddingError):
                embeddings.LocalEmbedder().query("企业资料")
        self.assertTrue(factory.call_args.kwargs["local_files_only"])
        self.opening.assert_not_called()

    def test_already_loaded_local_model_remains_available_without_network(self):
        model = MagicMock()
        model.query_embed.return_value = iter([SimpleNamespace(tolist=lambda: [1.0, 0.0])])
        embedder = embeddings.LocalEmbedder()
        embedder._model = model
        self.assertEqual(embedder.query("企业资料"), [1.0, 0.0])
        self.opening.assert_not_called()

    def test_missing_local_model_falls_back_to_keyword_results(self):
        chunk = Chunk("c", "d", "扫描器记录.md", "md", "", "扫码枪扫不上，重新配对恢复连接。")
        index = BM25Index().build([chunk])
        engine = retrieval_service.RetrievalService()
        factory = MagicMock(side_effect=FileNotFoundError("not installed"))
        with patch.dict(sys.modules, {"fastembed": SimpleNamespace(TextEmbedding=factory)}), \
                patch.object(retrieval_service.RetrievalService, "index", new_callable=PropertyMock, return_value=index), \
                patch.object(retrieval_service, "get_embedder", return_value=embeddings.LocalEmbedder()):
            hits = engine.search("扫码枪扫不上", use_rerank=False)
        self.assertEqual([hit.chunk.doc_id for hit in hits], ["d"])
        self.assertTrue(factory.call_args.kwargs["local_files_only"])
        self.opening.assert_not_called()

    def test_explicit_model_and_web_flags_cannot_force_external_answer_calls(self):
        hit = ScoredChunk(Chunk("c", "d", "年假制度.md", "md", "", "员工每年享有五天年假。"), 1.0, 1.0)
        with patch.object(answers, "settings", replace(settings, llm_api_key="test-key")), \
                patch.object(answers, "operations_mode", return_value=False), \
                patch.object(answers, "_top_hits", return_value=[hit]), \
                patch.object(answers, "uploaded_dates", return_value={}), \
                patch.object(answers, "rewrite_query") as rewrite, \
                patch.object(answers, "draft_solution") as draft, \
                patch.object(answers, "llm_complete") as complete, \
                patch.object(answers, "web_search") as web:
            response = answers.answer("年假有几天", [{"role": "user", "content": "年假制度"}],
                                      fast_mode=False, llm_polish=True, web_search=True)
        self.assertFalse(response.llm_used)
        self.assertIn("离线", response.notice)
        self.assertEqual([source.doc_id for source in response.sources], ["d"])
        for operation in (rewrite, draft, complete, web):
            operation.assert_not_called()
        self.opening.assert_not_called()

    def test_web_fallback_stays_local_when_runtime_web_flag_was_requested(self):
        with patch.object(answers, "web_search") as web:
            response = answers._web_fallback("网络异常", "网络问题", [], web_enabled=True)
        self.assertEqual(response.answer_origin, "none")
        self.assertIn("离线", response.notice)
        web.assert_not_called()

    def test_ingestion_and_weekly_reports_skip_model_calls_offline(self):
        with patch.object(analyzer, "settings", replace(settings, analysis_use_llm=True, llm_api_key="test-key")), \
                patch.object(analyzer, "chat") as analyze_model:
            result = analyzer.analyze([Block(text="报销申请需提交审批并保留票据。", heading="业务流程")], "报销流程.md")
        self.assertEqual(result.method, "rule")
        self.assertTrue(result.sections[0].summary)
        analyze_model.assert_not_called()
        with patch.object(weekly, "_record_content") as content, patch.object(weekly.llm_client, "chat") as report_model:
            self.assertEqual(weekly.summarize("employee", "2026-11-09", [{"doc_id": "d"}]), "")
        content.assert_not_called()
        report_model.assert_not_called()
        self.opening.assert_not_called()


class PolicyReadFailureTest(unittest.TestCase):
    def test_policy_reader_rejects_database_failures_and_non_boolean_results(self):
        for value in (False, "on", None):
            with self.subTest(value=value), \
                    patch.object(database, "external_apis_enabled", create=True, return_value=value):
                self.assertFalse(network_policy.external_apis_enabled())
        with patch.object(database, "external_apis_enabled", create=True, side_effect=RuntimeError("unavailable")):
            self.assertFalse(network_policy.external_apis_enabled())
        with patch.object(database, "external_apis_enabled", create=True, return_value=True):
            self.assertTrue(network_policy.external_apis_enabled())


if __name__ == "__main__":
    unittest.main()
