# -*- coding: utf-8 -*-
"""常识库检索行为：台账单元格值必须能被按值提问命中。"""
import unittest

from app.core.retrieval.bm25 import BM25Index
from app.domain.schemas import Chunk


def _chunk(cid: str, category: str, text: str, summary: str = "") -> Chunk:
    return Chunk(id=cid, doc_id="d1", doc_name="台账", doc_type="doc",
                 location="", text=text, heading="电脑台账",
                 summary=summary, category=category)


class KnowledgeRetrievalTest(unittest.TestCase):
    def test_knowledge_chunk_searches_by_cell_value(self):
        kn = _chunk("c1", "常识库", "编号: 74\n使用人: 张三\nIP: 10.10.0.66\n配置: i5/16G",
                    summary="本表为电脑台账；本段包含 1 条明细，具体字段值保留在原文中")
        index = BM25Index().build([kn])
        hits = index.search("10.10.0.66 是哪个电脑 谁在用", top_k=2)
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.id, "c1")

    def test_plain_chunk_still_summary_only(self):
        # 非常识库文档保持原设计：原文里的值不参与检索，只按摘要匹配
        plain = _chunk("c2", "MES系统", "正文里出现 10.10.0.66", summary="系统手册摘要，不含 IP")
        index = BM25Index().build([plain])
        self.assertEqual(index.search("10.10.0.66", top_k=2), [])


if __name__ == "__main__":
    unittest.main()
