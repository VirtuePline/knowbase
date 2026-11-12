import asyncio
import io
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pymongo import MongoClient
from fastapi import HTTPException, UploadFile

from app.api import routes
from app.config import settings
from app.domain.schemas import Chunk, DocRecord
from app.services import database, kb, preview

_MONGO_URI = os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017")


@contextmanager
def temp_workspace():
    """把所有 app.* 模块的 settings 指向临时目录与独立测试库，避免写进真实知识库。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mongo_client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")   # 本地没有 MongoDB 时尽早失败
        test_db = f"kbtest_{uuid4().hex[:8]}"
        tmp_settings = replace(
            settings,
            raw_dir=root / "raw",
            index_dir=root / "index",
            chunks_dir=root / "index" / "chunks",
            index_file=root / "index" / "bm25.json",
            images_dir=root / "images",
            analysis_use_llm=False,
            embedding_enabled=False,               # 测试不加载向量模型、不写真实向量库
            mongo_uri=_MONGO_URI,
            mongo_db=test_db,
        )
        for path in (tmp_settings.raw_dir, tmp_settings.chunks_dir, tmp_settings.images_dir):
            path.mkdir(parents=True, exist_ok=True)

        patched = {}
        for name, module in list(sys.modules.items()):
            if name.startswith("app.") and getattr(module, "settings", None) is settings:
                patched[name] = module
                module.settings = tmp_settings
        try:
            database.init_db()
            yield tmp_settings
        finally:
            for module in patched.values():
                module.settings = settings
            mongo_client.drop_database(test_db)
            mongo_client.close()


def _table_chunk(seq: int, rows: list[list[str]], heading: str = "故障维修记录") -> Chunk:
    return Chunk(
        id=f"c{seq}", doc_id="d1", doc_name="台账.xlsx", doc_type="xlsx",
        location=f"工作表「{heading}」 第{seq + 1}-{seq + 2}行",
        text="\n".join(" | ".join(r) for r in rows), heading=heading, table=rows,
    )


class MergePreviewTest(unittest.TestCase):
    def test_table_chunks_share_one_header(self):
        header = ["编号", "故障现象", "处理方案"]
        chunks = [
            _table_chunk(0, [header, ["WX-1", "卡料", "清理轨道"]]),
            _table_chunk(1, [header, ["WX-2", "偏移", "重新对中"]]),
            _table_chunk(2, [header, ["WX-3", "异响", "更换轴承"]]),
        ]
        sections = preview.merge_preview(chunks)

        self.assertEqual(len(sections), 1)
        table = sections[0]["table"]
        self.assertEqual(table[0], header, "合并后只保留一行表头")
        self.assertEqual([r[0] for r in table[1:]], ["WX-1", "WX-2", "WX-3"])
        self.assertEqual(sections[0]["kind"], "table")

    def test_different_sheets_are_not_merged(self):
        header = ["编号", "故障现象"]
        chunks = [
            _table_chunk(0, [header, ["A-1", "x"]], heading="贴标机"),
            _table_chunk(1, [header, ["B-1", "y"]], heading="灌装机"),
        ]
        sections = preview.merge_preview(chunks)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[1]["heading"], "灌装机")

    def test_text_chunks_keep_order_and_kind(self):
        text_chunks = [
            Chunk(id="t1", doc_id="d2", doc_name="记录.md", doc_type="md",
                  location="第1段", text="## 现象\n报工提示工单不存在。", heading="现象"),
            Chunk(id="t2", doc_id="d2", doc_name="记录.md", doc_type="md",
                  location="第2段", text="## 处理\n重建工单缓存。", heading="处理"),
        ]
        sections = preview.merge_preview(text_chunks)
        self.assertEqual([s["kind"] for s in sections], ["text", "text"])
        self.assertIn("重建工单缓存", sections[1]["text"])

    def test_over_long_document_appends_notice(self):
        chunks = [
            Chunk(id=f"t{i}", doc_id="d3", doc_name="长文.md", doc_type="md",
                  location="", text="故障" * 50, heading="")
            for i in range(10)
        ]
        with patch.object(preview, "MAX_PREVIEW_CHARS", 300):
            sections = preview.merge_preview(chunks)
        self.assertEqual(sections[-1]["kind"], "notice")


class MarkdownEditTest(unittest.TestCase):
    def setUp(self):
        self._workspace = temp_workspace()
        self._workspace.__enter__()

    def tearDown(self):
        self._workspace.__exit__(None, None, None)

    def test_create_markdown_doc(self):
        content = "# 贴标机故障记录\n\n## E-1024\n现象：标签偏移。\n处理：重新对中标签卷。\n"
        result = kb.save_markdown(content, "", "贴标机记录.md")

        self.assertFalse(result["duplicated"])
        self.assertTrue(result["created"])
        self.assertEqual(result["doc"]["name"], "贴标机记录")   # 记录名称不带 .md
        self.assertEqual(result["doc"]["status"], "ready")
        self.assertGreaterEqual(result["doc"]["chunks"], 1)

        doc_id = result["doc"]["id"]
        source = kb.read_source(doc_id)
        self.assertIn("重新对中标签卷", source["content"])

        detail = kb.preview(doc_id)
        self.assertTrue(detail["doc"]["editable"])
        self.assertTrue(detail["sections"])

    def test_filename_without_suffix_saved_as_md(self):
        result = kb.save_markdown("# 记录\n处理：更换传感器。", "", "点检记录")
        self.assertEqual(result["doc"]["name"], "点检记录")          # 名称不带后缀
        self.assertEqual(result["doc"]["ext"], ".md")               # 默认保存为 md 文件
        self.assertTrue(kb.source_file(result["doc"]["id"])[0].name.endswith(".md"))

    def test_filename_with_suffix_strips_it(self):
        result = kb.save_markdown("# 记录\n处理：更换传感器。", "", "巡检台账.md")
        self.assertEqual(result["doc"]["name"], "巡检台账")          # 传入带后缀也会剥掉

    def test_set_document_time_updates_date_only(self):
        doc = kb.save_markdown("# 记录\n处理：更换传感器。", "", "时间记录")["doc"]
        updated = kb.set_document_time(doc["id"], "2026-11-01")

        self.assertEqual(updated["uploaded_at"][:10], "2026-11-01")
        self.assertEqual(updated["uploaded_at"][-8:], doc["uploaded_at"][-8:])   # 时分秒保留
        self.assertEqual(kb.get_document(doc["id"])["uploaded_at"][:10], "2026-11-01")

    def test_set_document_time_rejects_invalid(self):
        doc = kb.save_markdown("# 记录\n处理：更换传感器。", "", "时间校验")["doc"]
        for bad in ("2026-13-01", "20261101", "2026-11-4", ""):
            with self.assertRaises(ValueError, msg=bad):
                kb.set_document_time(doc["id"], bad)

    def test_update_keeps_id_and_chunks_refreshed(self):
        created = kb.save_markdown("# 记录\n现象：异响。", "", "记录.md")["doc"]
        updated = kb.save_markdown(
            "# 记录\n现象：异响。\n处理：更换轴承。\n补充：复测温度 62℃。", created["id"], created["name"],
        )

        self.assertFalse(updated["duplicated"])
        self.assertFalse(updated["created"])
        self.assertEqual(updated["doc"]["id"], created["id"])
        self.assertEqual(updated["doc"]["uploaded_at"], created["uploaded_at"], "更新不改变首次上传时间")

        sections = kb.preview(created["id"])["sections"]
        self.assertIn("更换轴承", "".join(s.get("text", "") for s in sections))

    def test_same_content_is_rejected_as_duplicate(self):
        content = "# 重复记录\n处理：复位急停。"
        first = kb.save_markdown(content, "", "记录A.md")
        second = kb.save_markdown(content, "", "记录B.md")

        self.assertTrue(second["duplicated"])
        self.assertEqual(second["doc"]["id"], first["doc"]["id"])

    def test_duplicate_edit_does_not_delete_current_source(self):
        first = kb.save_markdown("# 文档甲\n保留甲的原文。", "", "甲.md")["doc"]
        second = kb.save_markdown("# 文档乙\n保留乙的原文。", "", "乙.md")["doc"]

        duplicate = kb.save_markdown("# 文档甲\n保留甲的原文。", second["id"], second["name"])

        self.assertTrue(duplicate["duplicated"])
        self.assertEqual(kb.read_source(second["id"])["content"], "# 文档乙\n保留乙的原文。")
        self.assertTrue(kb.get_document(second["id"])["has_source"])

    def test_replace_file_refreshes_content_and_keeps_id(self):
        created = kb.save_markdown("# 原记录\n处理：复位设备。", "", "原记录.md")["doc"]
        replaced = kb.replace_file(created["id"], "新记录.md", "# 新记录\n处理：更换传感器。\n".encode("utf-8"))

        self.assertTrue(replaced["replaced"])
        self.assertEqual(replaced["doc"]["id"], created["id"])
        self.assertEqual(replaced["doc"]["name"], "新记录.md")
        self.assertEqual(replaced["doc"]["status"], "ready")
        self.assertEqual(kb.read_source(created["id"])["content"], "# 新记录\n处理：更换传感器。\n")

    def test_invalid_replacement_preserves_original(self):
        created = kb.save_markdown("# 原记录\n处理：复位设备。", "", "原记录.md")["doc"]
        with self.assertRaises(ValueError):
            kb.replace_file(created["id"], "损坏文件.xlsx", b"not an excel workbook")

        self.assertEqual(kb.read_source(created["id"])["content"], "# 原记录\n处理：复位设备。")
        self.assertEqual(kb.get_document(created["id"])["status"], "ready")

    def test_empty_content_is_rejected(self):
        with self.assertRaises(ValueError):
            kb.save_markdown("   \n", "", "空.md")

    def test_non_editable_document_cannot_be_edited(self):
        record = DocRecord(
            id="pdf1", name="手册.pdf", stored_name="pdf1_手册.pdf", ext=".pdf",
            size=10, sha256="x" * 64, chunks=0, uploaded_at=database.now_iso(),
            status="ready",
        )
        database.add(record)

        self.assertFalse(kb.get_document("pdf1")["editable"])
        with self.assertRaises(ValueError):
            kb.read_source("pdf1")
        with self.assertRaises(ValueError):
            kb.save_markdown("# 覆盖", "pdf1", "手册.pdf")

    def test_missing_document_raises(self):
        with self.assertRaises(kb.DocumentNotFound):
            kb.preview("not-exist")


class ImageUploadTest(unittest.TestCase):
    def setUp(self):
        self._workspace = temp_workspace()
        self.tmp_settings = self._workspace.__enter__()

    def tearDown(self):
        self._workspace.__exit__(None, None, None)

    def test_image_is_saved_under_images_dir(self):
        upload = UploadFile(file=io.BytesIO(b"\x89PNG\r\n\x1a\n"), filename="示意图.png")
        with patch.object(routes.mongo, "db") as get_db:
            result = asyncio.run(routes.upload_image(upload, identity={"username": "engineer"}))
            get_db.return_value.image_assets.insert_one.assert_called_once()
        self.assertEqual(result["url"], "/images/" + result["name"])
        self.assertTrue((self.tmp_settings.images_dir / result["name"]).is_file())

    def test_non_image_extension_is_rejected(self):
        upload = UploadFile(file=io.BytesIO(b"hello"), filename="记录.txt")
        with self.assertRaises(HTTPException):
            asyncio.run(routes.upload_image(upload, identity={"username": "engineer"}))


if __name__ == "__main__":
    unittest.main()
