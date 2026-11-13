"""检索一致性回归；使用内存替身，不连接 MongoDB 或加载模型。"""
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, PropertyMock, patch

from app.core.retrieval import vector_store as vs
from app.core.retrieval.bm25 import BM25Index
from app.domain.schemas import Chunk, DocRecord, ScoredChunk
from app.services import kb
from app.services import retrieval_service as rs


def chunk(text="交换机丢包", cid="same-id"):
    return Chunk(id=cid, doc_id="doc", doc_name="记录.md", doc_type="md",
                 location="", text=text)


class MemoryVectors:
    def __init__(self):
        self.rows = {}

    def existing_hashes(self, model):
        return {cid: row.get("content_hash", "") for cid, row in self.rows.items()
                if row["model"] == model}

    def upsert(self, entries, model, content_hashes=None):
        for cid, did, vector in entries:
            self.rows[cid] = {"model": model, "vector": vector,
                              "content_hash": (content_hashes or {}).get(cid, "")}

    def prune(self, valid_ids):
        self.rows = {cid: row for cid, row in self.rows.items() if cid in valid_ids}


class VectorSyncTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryVectors()
        with patch.object(rs, "VectorStore", return_value=self.store):
            self.service = rs.RetrievalService()
        self.embedder = MagicMock()
        self.embedder.embed.side_effect = lambda texts: [
            [1.0, 0.0] if "丢包" in text else [0.0, 1.0] for text in texts]
        self.embed_patch = patch.object(rs, "get_embedder", return_value=self.embedder)
        self.embed_patch.start()
        self.addCleanup(self.embed_patch.stop)

    def sync(self, *chunks):
        self.service._sync_vectors(BM25Index().build(list(chunks)))

    def test_same_id_with_changed_content_replaces_vector(self):
        self.sync(chunk())
        old_hash = self.store.rows["same-id"]["content_hash"]
        self.sync(chunk("MES 接口超时"))
        self.assertEqual(self.embedder.embed.call_count, 2)
        self.assertEqual(self.store.rows["same-id"]["vector"], [0.0, 1.0])
        self.assertNotEqual(self.store.rows["same-id"]["content_hash"], old_hash)

    def test_unchanged_content_does_not_embed_again(self):
        self.sync(chunk())
        self.sync(chunk())
        self.embedder.embed.assert_called_once()

    def test_legacy_vector_without_hash_is_recomputed_once(self):
        self.store.rows["same-id"] = {"model": rs.settings.embedding_model,
                                      "vector": [0.0, 1.0]}
        self.sync(chunk())
        self.sync(chunk())
        self.embedder.embed.assert_called_once()
        self.assertEqual(self.store.rows["same-id"]["vector"], [1.0, 0.0])
        self.assertTrue(self.store.rows["same-id"]["content_hash"])

    def test_changed_summary_and_heading_are_embedded(self):
        original = replace(chunk(), heading="网络", summary="端口异常")
        self.sync(original)
        self.sync(replace(original, summary="报工异常"))
        self.sync(replace(original, summary="报工异常", heading="MES"))
        self.assertEqual(self.embedder.embed.call_count, 3)

    def test_changed_api_endpoint_recomputes_same_model(self):
        for endpoint in ("https://first.invalid/v1", "https://second.invalid/v1"):
            with patch.object(rs, "settings", replace(rs.settings,
                    embedding_provider="api", embedding_base_url=endpoint)):
                self.sync(chunk())
        self.assertEqual(self.embedder.embed.call_count, 2)

    def test_changed_model_recomputes_same_content(self):
        self.sync(chunk())
        with patch.object(rs, "settings", replace(rs.settings, embedding_model="new-model")):
            self.sync(chunk())
        self.assertEqual(self.embedder.embed.call_count, 2)
        self.assertEqual(self.store.rows["same-id"]["model"], "new-model")

    def test_deleted_chunks_are_pruned(self):
        self.sync(chunk(), chunk(cid="deleted"))
        self.sync(chunk())
        self.assertEqual(set(self.store.rows), {"same-id"})

    def test_incomplete_embedding_batch_is_not_saved(self):
        self.embedder.embed.side_effect = lambda texts: []
        self.sync(chunk())
        self.assertEqual(self.store.rows, {})
        self.assertIn("数量", self.service._vector_error)

    def test_bm25_recalls_full_rerank_window_before_truncating(self):
        index = MagicMock()
        index.search.return_value = [ScoredChunk(chunk(cid=str(i)), 1.0, 1.0)
                                     for i in range(5)]
        with patch.object(type(self.service), "index", new_callable=PropertyMock,
                          return_value=index), \
                patch.object(rs, "settings", replace(rs.settings, rerank_candidates=10)), \
                patch.object(rs, "get_embedder", return_value=None), \
                patch.object(rs, "get_reranker", return_value=None):
            hits = self.service.search("交换机", top_k=2)
        index.search.assert_called_once_with("交换机", top_k=10)
        self.assertEqual(len(hits), 2)


class LazyVectorStoreTest(unittest.TestCase):
    def test_construction_does_not_connect(self):
        with patch.object(vs.mongo, "db") as get_db:
            vs.VectorStore()
        get_db.assert_not_called()

    def test_first_use_ensures_index_once_and_tracks_database_changes(self):
        collection = MagicMock()
        collection.distinct.return_value = []
        database = MagicMock()
        database.name = "test-first"
        database.__getitem__.return_value = collection
        with patch.object(vs.mongo, "db", return_value=database):
            store = vs.VectorStore()
            store.existing_ids("model")
            store.existing_ids("model")
            self.assertEqual(collection.create_index.call_count, 1)
            database.name = "test-second"
            store.existing_ids("model")
            self.assertEqual(collection.create_index.call_count, 2)

    def test_legacy_hashes_are_empty_and_new_hashes_are_persisted(self):
        collection = MagicMock()
        collection.find.return_value = [{"_id": "legacy"}, {"_id": "new", "content_hash": "digest"}]
        database = MagicMock()
        database.name = "test"
        database.__getitem__.return_value = collection
        with patch.object(vs.mongo, "db", return_value=database):
            store = vs.VectorStore()
            self.assertEqual(store.existing_hashes("model"), {"legacy": "", "new": "digest"})
            store.upsert([("new", "doc", [1, 0])], "model", {"new": "digest"})
            operation = collection.bulk_write.call_args.args[0][0]
            self.assertEqual(operation._doc["content_hash"], "digest")
            self.assertEqual(operation._doc["vector"], [1.0, 0.0])
            # 保留旧的三元组调用方式；缺少哈希时下次同步会补算。
            store.upsert([("legacy", "doc", [0, 1])], "model")
            self.assertEqual(collection.bulk_write.call_args.args[0][0]._doc["content_hash"], "")


class ReadOnlyCategoryTest(unittest.TestCase):
    def record(self, **changes):
        record = DocRecord(id="doc", name="MES报工记录.md", stored_name="doc.md",
                           ext=".md", size=1, sha256="hash", chunks=1,
                           uploaded_at="2026-11-09", status="ready", doc_type="doc")
        return replace(record, **changes)

    def test_missing_category_is_derived_without_database_write(self):
        original = self.record()
        with patch.object(kb.Ingester, "read_chunks", return_value=[chunk()]), \
                patch.object(kb, "classify_document", return_value="MES系统"), \
                patch.object(kb.database, "set_category") as write:
            filled = kb._with_categories([original])
        write.assert_not_called()
        self.assertEqual(original.category, "")
        self.assertEqual(filled[0].category, "MES系统")

    def test_manual_category_does_not_get_reclassified(self):
        original = self.record(category="流程问题")
        with patch.object(kb, "classify_document") as classify:
            self.assertEqual(kb._with_categories([original]), [original])
        classify.assert_not_called()

    def test_document_counts_include_knowledge_like_document_list(self):
        docs = [self.record(category="常识库"), self.record(id="mes", category="MES系统"),
                self.record(id="record", category="硬件问题", doc_type="record")]
        with patch.object(kb.database, "list_all", return_value=docs), \
                patch.object(kb.database, "department_names", return_value={"operations": "默认部门"}):
            counts = {row["name"]: row["count"] for row in kb.category_counts("doc")}
            listed = kb.list_documents(doc_type="doc")
        self.assertEqual(sum(counts.values()), len(listed))
        self.assertEqual(counts["常识库"], 1)
        self.assertEqual(counts["硬件问题"], 0)


if __name__ == "__main__":
    unittest.main()
