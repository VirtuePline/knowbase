import unittest

from app.core.retrieval.bm25 import BM25Index
from app.core.retrieval.tokenizer import query_terms, tokenize
from app.domain.schemas import Chunk


def make_chunk(cid: str, doc_id: str, text: str, location: str = "") -> Chunk:
    return Chunk(id=cid, doc_id=doc_id, doc_name=f"{doc_id}.xlsx", doc_type="xlsx",
                 location=location, text=text)


class TokenizerTest(unittest.TestCase):
    def test_keeps_identifiers_intact(self):
        tokens = tokenize("贴标机 ZX-200 报 E-1024，MES版本 3.2.1")
        self.assertIn("zx-200", tokens)
        self.assertIn("e-1024", tokens)
        self.assertIn("3.2.1", tokens)

    def test_chinese_bigrams(self):
        tokens = tokenize("伺服驱动器插头松动")
        self.assertIn("伺服", tokens)
        self.assertIn("松动", tokens)

    def test_stopwords_removed(self):
        self.assertNotIn("怎么", query_terms("怎么处理"))


class BM25Test(unittest.TestCase):
    def setUp(self):
        self.index = BM25Index()
        self.index.build([
            make_chunk("c1", "docA", "贴标机报 E-1024，伺服驱动器插头松动，重新插紧后恢复", "第2行"),
            make_chunk("c2", "docA", "贴标机标签偏移，清洁光纤并校准电眼", "第3行"),
            make_chunk("c3", "docB", "MES 报工提示工单不存在，工单已关闭，重新开立工单"),
            make_chunk("c4", "docB", "MES 与 WMS 接口同步超时，连接池占满"),
        ])

    def test_relevant_chunk_ranked_first(self):
        hits = self.index.search("贴标机 E-1024 怎么处理", top_k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.id, "c1")
        self.assertGreaterEqual(hits[0].coverage, 0.3)

    def test_unrelated_question_has_low_coverage(self):
        hits = self.index.search("食堂晚餐几点开饭", top_k=3)
        if hits:
            self.assertLess(hits[0].coverage, 0.32)

    def test_empty_index_returns_no_hits(self):
        empty = BM25Index()
        empty.build([])
        self.assertEqual(empty.search("任意问题"), [])

    def test_save_and_load_roundtrip(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bm25.json"
            self.index.save(path)
            loaded = BM25Index.load(path)
        self.assertEqual(len(loaded.chunks), 4)
        self.assertEqual(loaded.search("WMS 接口超时", top_k=1)[0].chunk.id, "c4")

    def test_missing_index_file_is_empty(self):
        from pathlib import Path

        self.assertEqual(BM25Index.load(Path("不存在/index.json")).chunks, [])

    def test_analyzed_chunk_searches_summary_not_source_text(self):
        chunk = make_chunk("summary", "docSummary", "原文独有标识 ZX-200，详细处理过程保留用于溯源")
        chunk.summary = "故障码 E-1024：检查伺服驱动器并重新插紧接头"
        index = BM25Index().build([chunk])

        self.assertEqual(index.search("ZX-200"), [], "原文中的独有词不参与摘要索引")
        hits = index.search("E-1024")
        self.assertEqual(len(hits), 1)
        self.assertIn("ZX-200", hits[0].chunk.text, "命中后仍返回完整章节原文")

    def test_legacy_chunk_without_summary_still_searches_text(self):
        chunk = make_chunk("legacy", "oldDoc", "旧版分块 MES-1024 接口超时")
        index = BM25Index().build([chunk])
        self.assertEqual(len(index.search("MES-1024")), 1)


if __name__ == "__main__":
    unittest.main()
