"""企业知识检索的权限边界；只使用随机临时库与确定性模型替身。"""
import os
import unittest
from dataclasses import replace
from unittest.mock import PropertyMock, patch
from uuid import uuid4

from pymongo import MongoClient

from app.core.answer import references, service as answers
from app.core.retrieval.bm25 import BM25Index
from app.core.retrieval.vector_store import VectorStore
from app.domain.schemas import Block, Chunk, DocRecord, ScoredChunk
from app.services import access, database, mongo
from app.services import retrieval_service as rs


HR = {"username": "hr-user", "role": "user", "department_ids": ["hr"],
      "department_roles": {"hr": "editor"}}


def chunk(doc_id, text="年假制度：员工每年享有五天年假。", refs=None):
    return Chunk(doc_id + "-chunk", doc_id, doc_id + ".md", "md", "制度说明", text,
                 references=refs or [])


def document(doc_id, department="hr", visibility="department", shared=None):
    return DocRecord(doc_id, doc_id + ".md", doc_id + ".md", ".md", 100, doc_id,
                     1, "2026-11-12 09:00:00", "ready", summary="年假制度",
                     department_id=department, visibility=visibility,
                     shared_department_ids=shared or [])


class AllowedKeywordSearchTest(unittest.TestCase):
    def test_permissions_are_applied_before_keyword_top_k(self):
        private = [chunk(f"private-{number}", "年假制度 " * 20) for number in range(20)]
        public = chunk("public", "年假制度 五天")
        index = BM25Index().build([*private, public])
        hits = index.search("年假制度", top_k=1, allowed_doc_ids={"public"})
        self.assertEqual([hit.chunk.doc_id for hit in hits], ["public"])
        self.assertEqual(index.search("年假制度", allowed_doc_ids=set()), [])


class EnterpriseRetrievalTest(unittest.TestCase):
    def setUp(self):
        self.client = MongoClient(os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017"),
                                  serverSelectionTimeoutMS=5000)
        self.addCleanup(self.client.close)
        self.client.admin.command("ping")
        name = "kbtest_enterprise_retrieval_" + uuid4().hex
        self.addCleanup(self.client.drop_database, name)
        self.db = self.client[name]
        db_patch = patch.object(mongo, "db", return_value=self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        self.db.departments.insert_many([
            {"_id": "operations", "name": "默认部门"}, {"_id": "hr", "name": "人事部门"},
            {"_id": "finance", "name": "财务部门"},
        ])
        self.scope = access.identity_scope(HR)
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)

    def add(self, doc_id, **kwargs):
        record = document(doc_id, **kwargs)
        database.add(record)
        return record

    def test_hybrid_search_scopes_keyword_vector_and_rerank_before_top_k(self):
        chunks = []
        vectors = []
        for number in range(20):
            doc_id = f"private-{number}"
            self.add(doc_id, department="finance")
            chunks.append(chunk(doc_id))
            vectors.append((doc_id + "-chunk", doc_id, [1.0, 0.0]))
        self.add("hr")
        chunks.append(chunk("hr"))
        vectors.append(("hr-chunk", "hr", [0.7, 0.7]))
        index = BM25Index().build(chunks)
        retrieval = rs.RetrievalService()
        retrieval._vectors.upsert(vectors, rs.settings.embedding_model)
        with patch.object(rs.RetrievalService, "index", new_callable=PropertyMock, return_value=index), \
                patch.object(rs, "get_embedder") as embedder, \
                patch.object(rs, "get_reranker") as reranker:
            embedder.return_value.query.return_value = [1.0, 0.0]
            reranker.return_value.score.return_value = [0.9]
            hits = retrieval.search("年假制度", top_k=1)
        self.assertEqual([hit.chunk.doc_id for hit in hits], ["hr"])
        scored_texts = reranker.return_value.score.call_args.args[1]
        self.assertEqual(len(scored_texts), 1)
        # 即使本次索引仍含旧块，撤权立即作用于下一次查询。
        self.db.documents.update_one({"_id": "hr"}, {"$set": {"department_id": "finance"}})
        with patch.object(rs.RetrievalService, "index", new_callable=PropertyMock) as get_index:
            self.assertEqual(retrieval.search("年假制度", top_k=1), [])
            get_index.assert_not_called()

    def test_vector_acl_does_not_reuse_global_or_previous_user_faiss_results(self):
        store = VectorStore()
        store.upsert([("secret-c", "secret", [1.0, 0.0]),
                      ("public-c", "public", [0.5, 0.8])], "model")
        self.assertEqual(store.search([1.0, 0.0], 1, "model")[0][0], "secret-c")
        self.assertEqual(store.search([1.0, 0.0], 1, "model", {"public"})[0][0], "public-c")
        self.assertEqual(store.search([1.0, 0.0], 1, "model", set()), [])

    def test_scope_contains_public_own_and_explicitly_shared_documents_only(self):
        self.add("own")
        self.add("public", department="operations", visibility="public")
        self.add("shared", department="finance", shared=["hr"])
        self.add("private", department="finance")
        self.assertEqual(rs.allowed_document_ids(), {"own", "public", "shared"})
        with access.identity_scope(None):
            self.assertEqual(rs.allowed_document_ids(), set())

    def test_public_parent_cannot_expand_private_references(self):
        self.add("public", department="operations", visibility="public")
        self.add("secret", department="finance")
        hit = ScoredChunk(chunk("public", refs=["secret"]), 1.0, 1.0)
        with patch.object(references, "load_blocks", return_value=[Block("辅助内容")]) as load:
            sources = answers._sources([hit])
            context = references.append_references("【记录 1】公共制度", [hit])
            self.assertEqual(sources[0].references, [])
            self.assertEqual(context, "【记录 1】公共制度")
            load.assert_not_called()
            self.db.documents.update_one({"_id": "secret"}, {"$set": {"shared_department_ids": ["hr"]}})
            self.assertEqual(answers._sources([hit])[0].references[0].doc_id, "secret")
            self.assertIn("辅助内容", references.append_references("【记录 1】公共制度", [hit]))

    def test_generic_qa_does_not_apply_fault_rules_or_untrusted_browser_history(self):
        self.add("hr")
        hit = ScoredChunk(chunk("hr"), 1.0, 1.0)
        history = [{"role": "assistant", "content": "无权读取的旧财务信息"}]
        config = replace(answers.settings, llm_api_key="test")
        with patch.object(answers, "settings", config), \
                patch.object(answers, "_top_hits", return_value=[hit]), \
                patch.object(answers, "filter_fault_hits") as fault_filter, \
                patch.object(answers, "rewrite_query", return_value="年假有几天"), \
                patch.object(answers, "draft_solution") as draft, \
                patch.object(answers, "llm_complete", return_value="每年五天。") as complete:
            response = answers.answer("年假有几天", history, fast_mode=False, llm_polish=True)
            self.assertEqual(response.status, "ok")
            self.assertEqual(response.answer, "每年五天。")
            self.assertEqual(complete.call_args.args[2], [])
            fault_filter.assert_not_called()
            draft.assert_not_called()
            with access.trusted_history_scope():
                answers.answer("年假有几天", history, fast_mode=False, llm_polish=True)
            self.assertEqual(complete.call_args.args[2], history)

    def test_revocation_during_model_call_discards_the_old_answer(self):
        self.add("hr")
        hit = ScoredChunk(chunk("hr"), 1.0, 1.0)

        def revoke(*args):
            self.db.documents.update_one({"_id": "hr"}, {"$set": {"department_id": "finance"}})
            return "撤权后的旧内容"

        with patch.object(answers, "settings", replace(answers.settings, llm_api_key="test")), \
                patch.object(answers, "_top_hits", return_value=[hit]), \
                patch.object(answers, "llm_complete", side_effect=revoke):
            response = answers.answer("年假有几天", fast_mode=False, llm_polish=True)
        self.assertEqual(response.sources, [])
        self.assertNotIn("旧内容", response.answer)
        self.assertIn("权限已变更", response.answer)


if __name__ == "__main__":
    unittest.main()
