"""「仅说明」引用不入检索索引：load_blocks 行级过滤。"""
import tempfile
import unittest
from pathlib import Path

from app.core.ingest.loaders import load_blocks

MD = """# 处理记录

现象：网页打不开，重启光猫后恢复。

> 参考（仅说明）：[《设备手册》](doc://abc123)

> 参考：[《网络规划》](doc://xyz789)

> 参考（仅说明）：[《另一份手册》](doc://only1)
"""


class NoRefLineTest(unittest.TestCase):
    def test_no_ref_lines_are_dropped_from_index_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "记录.md"
            path.write_text(MD, encoding="utf-8")
            blocks = load_blocks(path)
        text = "\n".join(b.text for b in blocks)
        # 「仅说明」引用被剔除，普通引用与正文保留
        self.assertNotIn("doc://abc123", text)
        self.assertNotIn("仅说明", text)
        self.assertIn("doc://xyz789", text)
        self.assertIn("重启光猫后恢复", text)

    def test_block_of_only_no_ref_lines_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "记录.md"
            path.write_text(
                "# 章节\n\n正文\n\n> 参考（仅说明）：[《A》](doc://a1)\n\n> 参考（仅说明）：[《B》](doc://a2)\n",
                encoding="utf-8")
            blocks = load_blocks(path)
        self.assertEqual([b.text for b in blocks if b.text == "正文"], ["正文"])
        self.assertFalse(any("doc://a1" in b.text for b in blocks))


if __name__ == "__main__":
    unittest.main()
