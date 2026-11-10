import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pymongo import MongoClient

from app.config import settings
from app.core.retrieval import embeddings as emb_module
from app.core.retrieval.bm25 import BM25Index
from app.core.retrieval.vector_store import VectorStore
from app.domain.schemas import Chunk
from app.services import mongo as mongo_mod
from app.services import retrieval_service as rs

_MONGO_URI = os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017")


def make_chunk(cid: str, text: str) -> Chunk:
    return Chunk(id=cid, doc_id="d1", doc_name="记录.md", doc_type="md", location="", text=text)


class FakeEmbedder:
    """确定性二维向量：含「丢包」的文本指向 x 轴，其余指向 y 轴。"""

    def embed(self, texts):
        return [[1.0, 0.0] if "丢包" in t else [0.0, 1.0] for t in texts]

    def query(self, text):
        return [1.0, 0.0]


class VectorStoreTest(unittest.TestCase):
    def setUp(self):
        self._mongo = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
        self._mongo.admin.command("ping")
        self._db = f"kbtest_{uuid4().hex[:8]}"
        self.store = VectorStore(mongo_uri=_MONGO_URI, mongo_db=self._db)

    def tearDown(self):
        self._mongo.drop_database(self._db)
        self._mongo.close()

    def test_upsert_search_and_prune(self):
        self.store.upsert([("a", "d1", [1.0, 0.0]), ("b", "d1", [0.0, 1.0])], "m")
        self.assertEqual(self.store.count("m"), 2)
        self.assertEqual(self.store.existing_ids("m"), {"a", "b"})

        hits = self.store.search([1.0, 0.0], 2, "m")
        self.assertEqual(hits[0][0], "a")
        self.assertAlmostEqual(hits[0][1], 1.0, places=5)
        self.assertLess(hits[1][1], 0.01)

        self.store.prune({"a"})
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.search([1.0, 0.0], 5, "m")[0][0], "a")

    def test_models_are_isolated(self):
        self.store.upsert([("a", "d1", [1.0, 0.0])], "m1")
        self.assertEqual(self.store.search([1.0, 0.0], 5, "m2"), [])
        self.assertEqual(self.store.count("m2"), 0)


class HybridSearchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._mongo = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
        self._db = f"kbtest_{uuid4().hex[:8]}"
        self.tmp_settings = replace(
            settings,
            index_file=root / "bm25.json",
            mongo_uri=_MONGO_URI,
            mongo_db=self._db,
            embedding_model="fake-model",
        )
        chunks = [make_chunk("cA", "MES 报工接口超时"), make_chunk("cB", "交换机端口丢包")]
        BM25Index().build(chunks).save(self.tmp_settings.index_file)
        self._settings_patch = patch.object(rs, "settings", self.tmp_settings)
        self._settings_patch.start()
        # VectorStore 经 mongo 模块取库，其 settings 也要指向测试库
        self._mongo_patch = patch.object(mongo_mod, "settings", self.tmp_settings)
        self._mongo_patch.start()
        # 默认不做 rerank，避免测试加载真实模型
        self._rr_patch = patch.object(rs, "get_reranker", return_value=None)
        self._rr_patch.start()
        self.service = rs.RetrievalService()   # 向量库写入测试库

    def tearDown(self):
        self._rr_patch.stop()
        self._mongo_patch.stop()
        self._settings_patch.stop()
        self._mongo.drop_database(self._db)
        self._mongo.close()
        self._tmp.cleanup()

    def test_semantic_hit_merges_with_bm25(self):
        with patch.object(rs, "get_embedder", return_value=FakeEmbedder()):
            hits = self.service.search("MES 报工", top_k=4)
        by_id = {h.chunk.id: h for h in hits}
        self.assertIn("cA", by_id, "关键词命中保留")
        self.assertIn("cB", by_id, "无关键词重叠但语义相近的分块也被召回")
        self.assertGreater(by_id["cA"].coverage, 0)
        self.assertEqual(by_id["cB"].coverage, 0.0)
        self.assertGreater(by_id["cB"].semantic, 0.9)
        self.assertEqual(hits[0].chunk.id, "cA", "两路都命中的分块排最前")

    def test_disabled_embedding_falls_back_to_bm25(self):
        with patch.object(rs, "get_embedder", return_value=None):
            hits = self.service.search("MES 报工", top_k=4)
        self.assertEqual([h.chunk.id for h in hits], ["cA"])

    def test_embedding_error_falls_back_to_bm25(self):
        class BadEmbedder:
            def embed(self, texts):
                raise emb_module.EmbeddingError("接口不可用")

            def query(self, text):
                raise emb_module.EmbeddingError("接口不可用")

        with patch.object(rs, "get_embedder", return_value=BadEmbedder()):
            hits = self.service.search("MES 报工", top_k=4)
        self.assertEqual([h.chunk.id for h in hits], ["cA"])

    def test_rerank_reorders_candidates(self):
        class FakeReranker:
            def score(self, query, texts):
                return [0.9 if "丢包" in t else 0.05 for t in texts]

        with patch.object(rs, "get_embedder", return_value=FakeEmbedder()), \
                patch.object(rs, "get_reranker", return_value=FakeReranker()):
            hits = self.service.search("MES 报工", top_k=2)
        self.assertEqual(hits[0].chunk.id, "cB", "rerank 概率最高者排第一")
        self.assertGreater(hits[0].rerank, 0.8)

    def test_rerank_error_falls_back_to_rrf(self):
        class BadReranker:
            def score(self, query, texts):
                raise rs.RerankError("模型不可用")

        with patch.object(rs, "get_embedder", return_value=FakeEmbedder()), \
                patch.object(rs, "get_reranker", return_value=BadReranker()):
            hits = self.service.search("MES 报工", top_k=2)
        self.assertEqual(hits[0].chunk.id, "cA", "rerank 失败时按 RRF 融合排序")
        self.assertEqual(hits[0].rerank, 0.0)

    def test_sync_embeds_only_new_chunks(self):
        embedder = FakeEmbedder()
        calls = []
        original = embedder.embed

        def counting(texts):
            calls.append(list(texts))
            return original(texts)

        embedder.embed = counting
        with patch.object(rs, "get_embedder", return_value=embedder):
            self.service._sync_vectors(self.service.index)
            self.assertEqual(sum(len(c) for c in calls), 2)
            self.service._sync_vectors(self.service.index)
            self.assertEqual(sum(len(c) for c in calls), 2, "已嵌入的分块不重复计算")
        self.assertEqual(self.service._vectors.count("fake-model"), 2)


if __name__ == "__main__":
    unittest.main()
