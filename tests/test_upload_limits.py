"""上传限制与事件循环隔离；无需数据库、模型或真实文件。"""
import io
import threading
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app.api import routes
from app.config import settings


def upload(name="record.md", content=b"MES timeout", mime="text/markdown"):
    return UploadFile(io.BytesIO(content), filename=name,
                      headers=Headers({"content-type": mime}))


class UploadLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_upload_reads_only_limit_plus_one(self):
        file = AsyncMock()
        file.read.return_value = b"12345"
        with self.assertRaises(HTTPException) as raised:
            await routes._read_upload(file, 4, "too large")
        self.assertEqual(raised.exception.status_code, 413)
        file.read.assert_awaited_once_with(5)

    async def test_document_at_limit_is_accepted(self):
        self.assertEqual(await routes._read_upload(upload(content=b"1234"), 4, "limit"), b"1234")

    async def test_ingestion_and_replacement_run_outside_event_loop(self):
        loop_thread = threading.get_ident()

        def ingest(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return {"ok": True}

        with patch.object(routes.kb, "upload", side_effect=ingest) as store:
            result = await routes.upload_document(upload(), "record", "MES系统", "", "", "", "",
                                                   department_id="operations", visibility="department",
                                                   shared_department_ids="[]",
                                                   identity={"username": "engineer", "role": "user"})
            self.assertEqual(result, {"ok": True})
            self.assertEqual(store.call_args.kwargs["doc_type"], "record")
            self.assertEqual(store.call_args.kwargs["uploaded_by"], "engineer")
            self.assertEqual(await routes.upload_knowledge(upload(), department_id="operations",
                             visibility="department", shared_department_ids="[]",
                             identity={"username": "engineer"}), {"ok": True})
        with patch.object(routes.kb, "replace_file", side_effect=ingest):
            self.assertEqual(await routes.replace_document("doc1", upload()), {"ok": True})

    async def test_oversized_document_never_enters_ingestion(self):
        with patch.object(routes.kb, "MAX_UPLOAD_BYTES", 4), patch.object(routes.kb, "upload") as store:
            with self.assertRaises(HTTPException) as raised:
                await routes.upload_document(upload(content=b"12345"), "doc", "", "", "")
            self.assertEqual(raised.exception.status_code, 413)
            store.assert_not_called()

    async def test_image_recognition_runs_outside_event_loop(self):
        loop_thread = threading.get_ident()

        def describe(data, mime):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return "MES timeout"

        with patch.object(routes, "settings", replace(settings, vision_api_key="test")), \
                patch.object(routes.llm, "describe_image", side_effect=describe):
            result = await routes.chat_image(upload("error.png", b"image", "image/png"))
        self.assertEqual(result, {"question": "MES timeout"})


if __name__ == "__main__":
    unittest.main()
