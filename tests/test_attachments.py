"""记录附件：只存附件目录，不进文档库、不切分、不参与索引。"""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import kb


class AttachmentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store: list[dict] = []
        self.settings = replace(kb.settings, attachments_dir=self.dir)
        self.patches = [
            patch.object(kb, "settings", self.settings),
            patch.object(kb, "_require", return_value=SimpleNamespace(id="d1")),
            patch.object(kb.database, "get_attachments", side_effect=lambda d: list(self.store)),
            patch.object(kb.database, "add_attachments",
                         side_effect=lambda d, entries: self.store.extend(entries)),
            patch.object(kb.database, "remove_attachment",
                         side_effect=lambda d, stored: self.store.__setitem__(
                             slice(None), [x for x in self.store if x["stored"] != stored])),
            patch.object(kb.database, "now_iso", return_value="2026-11-12 10:00:00"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self._tmp.cleanup()

    def test_add_attachments_stores_file_and_metadata(self):
        result = kb.add_attachments("d1", [("故障照片.png", b"png-bytes")], uploaded_by="admin")
        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(entry["name"], "故障照片.png")
        self.assertEqual(entry["size"], 9)
        self.assertEqual(entry["uploaded_by"], "admin")
        stored_path = self.dir / "d1" / entry["stored"]
        self.assertTrue(stored_path.is_file())
        self.assertEqual(stored_path.read_bytes(), b"png-bytes")

    def test_attachment_not_in_document_store(self):
        """附件只写在 attachments 目录，不落 data/raw，天然不进切分与索引。"""
        before = {p.name for p in self.settings.raw_dir.iterdir() if p.is_file()}
        kb.add_attachments("d1", [("台账.xlsx", b"xlsx")])
        after = {p.name for p in self.settings.raw_dir.iterdir() if p.is_file()}
        self.assertEqual(before, after)                 # 原始文档目录没有新增文件
        self.assertTrue(list((self.dir / "d1").iterdir()))   # 附件只落在附件目录
        self.assertEqual(self.store[0]["name"], "台账.xlsx")  # 元数据挂在记录上

    def test_rejects_executable(self):
        with self.assertRaises(ValueError) as ctx:
            kb.add_attachments("d1", [("setup.exe", b"MZ")])
        self.assertIn("可执行文件", str(ctx.exception))

    def test_invalid_later_attachment_does_not_write_the_valid_prefix(self):
        with self.assertRaises(ValueError):
            kb.add_attachments("d1", [("日志.txt", b"log"), ("bad.exe", b"MZ")])
        self.assertEqual(self.store, [])
        self.assertEqual(list(self.dir.rglob("*")), [])

    def test_metadata_failure_removes_written_files(self):
        with patch.object(kb.database, "add_attachments", side_effect=RuntimeError("save failed")):
            with self.assertRaises(RuntimeError):
                kb.add_attachments("d1", [("a.pdf", b"pdf"), ("b.txt", b"text")])
        self.assertEqual(self.store, [])
        self.assertEqual(list((self.dir / "d1").iterdir()), [])

    def test_retry_of_committed_batch_does_not_create_duplicate_attachments(self):
        files = [("日志.txt", b"log"), ("照片.png", b"image")]
        first = kb.add_attachments("d1", files, batch_id="test-batch-1")
        again = kb.add_attachments("d1", files, batch_id="test-batch-1")
        self.assertEqual(first, again)
        self.assertEqual(len(self.store), 2)
        self.assertEqual(len(list((self.dir / "d1").iterdir())), 2)

    def test_reusing_batch_for_different_files_does_not_hide_the_new_version(self):
        kb.add_attachments("d1", [("日志.txt", b"old")], batch_id="test-batch-2")
        with self.assertRaises(ValueError):
            kb.add_attachments("d1", [("日志.txt", b"new")], batch_id="test-batch-2")
        self.assertEqual(len(self.store), 1)

    def test_rejects_oversized_file(self):
        big = b"0" * (kb.ATTACHMENT_MAX_BYTES + 1)
        with self.assertRaises(ValueError) as ctx:
            kb.add_attachments("d1", [("大文件.zip", big)])
        self.assertIn("20MB", str(ctx.exception))

    def test_rejects_too_many_attachments(self):
        files = [(f"a{i}.txt", b"x") for i in range(kb.ATTACHMENT_MAX_COUNT + 1)]
        with self.assertRaises(ValueError) as ctx:
            kb.add_attachments("d1", files)
        self.assertIn("最多", str(ctx.exception))

    def test_attachment_path_requires_registered_entry(self):
        kb.add_attachments("d1", [("照片.jpg", b"jpg")])
        stored = self.store[0]["stored"]
        path, name, media = kb.attachment_path("d1", stored)
        self.assertEqual(name, "照片.jpg")
        self.assertEqual(media, "image/jpeg")
        self.assertTrue(path.is_file())
        with self.assertRaises(kb.DocumentNotFound):
            kb.attachment_path("d1", "../../etc/passwd")

    def test_remove_attachment_deletes_file(self):
        kb.add_attachments("d1", [("a.pdf", b"pdf")])
        stored = self.store[0]["stored"]
        remaining = kb.remove_attachment("d1", stored)
        self.assertEqual(remaining, [])
        self.assertFalse((self.dir / "d1" / stored).exists())


if __name__ == "__main__":
    unittest.main()
