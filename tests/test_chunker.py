import unittest

from app.core.ingest.analyzer import table_summary
from app.core.ingest.chunker import build_chunks, split_table
from app.domain.schemas import Block


class ChunkerTest(unittest.TestCase):
    def test_excel_table_keeps_header_and_row_range(self):
        rows = [["编号", "故障现象", "处理方案"]] + [[f"WX-{i}", f"现象{i}", f"方案{i}"] for i in range(30)]
        block = Block(text="", table=rows, heading="故障维修记录", location="工作表「故障维修记录」")
        chunks = split_table(block, {"id": "d1", "name": "台账.xlsx", "type": "xlsx"}, 0)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.table[0], ["编号", "故障现象", "处理方案"], "每块必须重复表头")
        self.assertIn("第2-13行", chunks[0].location)
        self.assertEqual(chunks[0].doc_name, "台账.xlsx")

    def test_title_row_is_not_treated_as_header(self):
        """首行是合并标题（如"主机、显示器台账"）时：表头取第二行，标题并入分块标题。"""
        rows = ([["主机、显示器台账"], ["序号", "品名", "IP"]]
                + [[str(i), "主机", f"10.10.0.{i}"] for i in range(1, 31)])
        block = Block(text="", table=rows, heading="信息部资产台账", location="工作表「主机、显示器台账」")
        chunks = split_table(block, {"id": "d9", "name": "资产.xlsx", "type": "xlsx"}, 0)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.table[0], ["序号", "品名", "IP"], "字段行才是表头，标题行不能混入")
            self.assertTrue(all(row[0] != "主机、显示器台账" for row in chunk.table[1:]))
        self.assertEqual(chunks[0].table[1], ["1", "主机", "10.10.0.1"])
        self.assertIn("主机、显示器台账", chunks[0].heading)
        self.assertIn("第3-14行", chunks[0].location)   # 标题占第 1 行，数据从第 3 行开始

    def test_table_summary_keeps_structure_but_omits_row_values(self):
        rows = [
            ["编号", "故障现象", "运维人"],
            ["WX-001", "标签偏移", "刘工"],
            ["WX-002", "电机异响", "周工"],
        ]
        block = Block(text="", table=rows, heading="故障维修记录")
        block.summary = table_summary(block)
        chunks = split_table(block, {"id": "d4", "name": "台账.xlsx", "type": "xlsx"}, 0)

        joined = "\n".join(chunk.summary for chunk in chunks)
        self.assertIn("故障维修记录", joined)
        self.assertIn("设备编号" if "设备编号" in joined else "编号", joined)
        self.assertNotIn("WX-001", joined)
        self.assertNotIn("标签偏移", joined)
        self.assertNotIn("刘工", joined)

    def test_text_chunks_cover_whole_document(self):
        text = "。".join(f"第{i}条故障描述与处理方案" for i in range(200)) + "。"
        block = Block(text=text, heading="操作规范")
        chunks = build_chunks([block], {"id": "d2", "name": "规范.md", "type": "md"})

        self.assertGreater(len(chunks), 1)
        joined = "".join(c.text for c in chunks)
        for marker in ("第0条", "第100条", "第199条"):
            self.assertIn(marker, joined)
        self.assertTrue(all(c.doc_id == "d2" for c in chunks))
        self.assertTrue(all(len(c.id) == 16 for c in chunks))

    def test_empty_block_produces_nothing(self):
        self.assertEqual(build_chunks([Block(text="   ")], {"id": "d3", "name": "x", "type": "txt"}), [])


if __name__ == "__main__":
    unittest.main()
