import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.core.ingest.loaders import _pdf_outline_titles, load_blocks


class PdfOutlineTest(unittest.TestCase):
    def test_outline_title_path_and_page_label_filter(self):
        overview = SimpleNamespace(title="1 概述")
        detail = SimpleNamespace(title="1.1 参数")
        page_label = SimpleNamespace(title="第2页")
        pages = {id(overview): 0, id(detail): 1, id(page_label): 2}

        class Reader:
            outline = [overview, [detail], page_label]

            @staticmethod
            def get_destination_page_number(item):
                return pages[id(item)]

        self.assertEqual(
            _pdf_outline_titles(Reader()),
            {0: "1 概述", 1: "1 概述 / 1.1 参数"},
        )


class TextLoaderTest(unittest.TestCase):
    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "记录.md"
            path.write_text(text, encoding="utf-8")
            return load_blocks(path)

    def test_markdown_sections_are_not_merged(self):
        blocks = self._load(
            "# MES 运维记录\n\n"
            "## 报工提示工单不存在\n现象：提交报工弹窗提示工单不存在。\n"
            "## 与 WMS 接口同步超时\n现象：入库单 30 分钟未到 WMS，接口日志报超时。\n",
        )
        headings = [b.heading for b in blocks]
        self.assertEqual(headings, ["报工提示工单不存在", "与 WMS 接口同步超时"])
        self.assertIn("WMS", blocks[1].text)
        self.assertNotIn("WMS", blocks[0].text)

    def test_same_section_paragraphs_merge(self):
        blocks = self._load("## 同一条记录\n现象：异响。\n处理：更换轴承。\n")
        self.assertEqual(len(blocks), 1)
        self.assertIn("更换轴承", blocks[0].text)

    def test_empty_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "空.txt"
            path.write_text("   \n\n", encoding="utf-8")
            with self.assertRaises(Exception):
                load_blocks(path)


if __name__ == "__main__":
    unittest.main()
