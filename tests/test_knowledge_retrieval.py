# -*- coding: utf-8 -*-
"""文档库按部门管理后，台账单元格值仍可按值检索。"""
import unittest

from app.core.retrieval.bm25 import BM25Index
from app.domain.schemas import Chunk
from app.services.retrieval_service import _embed_text


def _chunk(cid: str, category: str, text: str, summary: str = "", content_type: str = "record") -> Chunk:
    return Chunk(id=cid, doc_id="d1", doc_name="台账", doc_type="md", content_type=content_type,
                 location="", text=text, heading="电脑台账",
                 summary=summary, category=category)


class KnowledgeRetrievalTest(unittest.TestCase):
    def test_department_document_searches_by_cell_value_without_category(self):
        kn = _chunk("c1", "", "编号: 74\n使用人: 张三\nIP: 10.10.0.66\n配置: i5/16G",
                    summary="本表为电脑台账；本段包含 1 条明细，具体字段值保留在原文中", content_type="doc")
        index = BM25Index().build([kn])
        hits = index.search("10.10.0.66 是哪个电脑 谁在用", top_k=2)
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.id, "c1")
        self.assertIn("10.10.0.66", _embed_text(kn))

    def test_plain_chunk_still_summary_only(self):
        # 工作记录继续按摘要匹配。
        plain = _chunk("c2", "MES系统", "正文里出现 10.10.0.66", summary="系统手册摘要，不含 IP")
        index = BM25Index().build([plain])
        self.assertEqual(index.search("10.10.0.66", top_k=2), [])
        self.assertNotIn("10.10.0.66", _embed_text(plain))

    def test_old_knowledge_chunk_keeps_full_text_search_before_rebuild(self):
        legacy = _chunk("legacy", "常识库", "使用人张三 IP 10.10.0.66", "电脑台账摘要", content_type="")
        restored = Chunk.from_dict(legacy.to_dict())
        self.assertTrue(BM25Index().build([restored]).search("10.10.0.66"))
        self.assertIn("10.10.0.66", _embed_text(restored))


if __name__ == "__main__":
    unittest.main()
