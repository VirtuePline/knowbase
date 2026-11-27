"""独立工具上传、下载与索引隔离的回归测试。"""
import io
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import DEFAULT, MagicMock, patch
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import routes
from app.core.ingest import analyzer, pipeline
from app.services import access, database, kb, mongo, retrieval_service
from tests.test_retrieval_consistency import MemoryVectors


class ToolUploadTest(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.config = replace(kb.settings, raw_dir=self.root / "raw", index_dir=self.root / "index",
                              chunks_dir=self.root / "index" / "chunks",
                              index_file=self.root / "index" / "bm25.json",
                              attachments_dir=self.root / "attachments",
                              analysis_use_llm=False, embedding_enabled=True)
        self.config.raw_dir.mkdir()
        self.config.chunks_dir.mkdir(parents=True)
        for module in (kb, analyzer, pipeline, retrieval_service):
            stack.enter_context(patch.object(module, "settings", self.config))

        self.records = {}
        stack.enter_context(patch.multiple(
            database, add=DEFAULT, get=DEFAULT, list_all=DEFAULT,
            find_by_hash=DEFAULT, get_attachments=DEFAULT,
            department_names=DEFAULT, update_status=DEFAULT,
            clear_document_categories=DEFAULT, recorder_label=DEFAULT, add_edit_log=DEFAULT,
            set_document_access=DEFAULT, count_children=DEFAULT, delete=DEFAULT,
        ))
        database.add.side_effect = lambda doc: self.records.__setitem__(doc.id, doc)
        database.get.side_effect = self.records.get
        database.list_all.side_effect = lambda: list(self.records.values())
        database.find_by_hash.side_effect = self.find_by_hash
        database.update_status.side_effect = self.update_status
        database.get_attachments.return_value = []
        database.department_names.return_value = {"operations": "运维部", "finance": "财务部"}
        database.recorder_label.side_effect = lambda name, fallback: name
        database.set_document_access.side_effect = self.set_document_access
        database.count_children.return_value = 0
        database.delete.side_effect = lambda doc_id: self.records.pop(doc_id, None) is not None
        stack.enter_context(patch.object(mongo, "db"))

        self.vectors = MemoryVectors()
        with patch.object(retrieval_service, "VectorStore", return_value=self.vectors):
            self.retrieval = retrieval_service.RetrievalService()
        self.embedder = MagicMock()
        self.embedder.embed.side_effect = lambda texts: [[1.0, 0.0] for _ in texts]
        stack.enter_context(patch.object(retrieval_service, "get_embedder", return_value=self.embedder))
        stack.enter_context(patch.object(kb, "retrieval", self.retrieval))

        api = FastAPI()
        api.include_router(routes.search_router)
        api.include_router(routes.admin_router)
        api.dependency_overrides[routes.require_login] = lambda: {"username": "engineer"}
        self.api = self.enterContext(TestClient(api))

    def find_by_hash(self, digest, *, uploaded_by, parent_id, doc_type="", indexable=None):
        return next((doc for doc in self.records.values()
                     if doc.sha256 == digest and doc.uploaded_by == uploaded_by
                     and doc.parent_id == parent_id and (not doc_type or doc.doc_type == doc_type)
                     and (indexable is None or doc.indexable == indexable)), None)

    def update_status(self, doc_id, chunks, status, error="", **analysis):
        self.records[doc_id] = replace(self.records[doc_id], chunks=chunks, status=status,
                                       error=error, **analysis)

    def set_document_access(self, doc_id, **scope):
        self.records[doc_id] = replace(self.records[doc_id], **scope)

    def upload(self, name, data, path="/api/documents", **kwargs):
        return self.api.post(path, files={"file": (name, data)},
                             data={"department_id": "operations", **kwargs})

    def upload_tool(self, name, data, **kwargs):
        return self.upload(name, data, path="/api/tools", **kwargs)

    def replace_file(self, collection, doc_id, name, data):
        return self.api.put(f"/api/{collection}/{doc_id}/replace", files={"file": (name, data)})

    def test_tool_is_listed_and_downloaded_without_entering_search(self):
        name = "设备配置工具.EXE"
        content = b"MZ\x00\x01unique-private-installer-content"
        with patch.object(pipeline.Ingester, "ingest") as ingest, \
                patch.object(routes, "_read_upload", side_effect=AssertionError("whole-file read")):
            uploaded = self.upload_tool(name, content, file_time="2026-11-24 14:30:22")
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            doc = uploaded.json()["doc"]
            self.assertFalse(doc["indexed"])
            self.assertEqual(doc["status"], "ready")
            self.assertEqual(doc["chunks"], 0)
            self.assertEqual(doc["uploaded_at"], "2026-11-24 14:30:22")

            retry = self.upload_tool(name, content)
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertTrue(retry.json()["duplicated"])
            self.assertEqual(retry.json()["doc"]["id"], doc["id"])
            self.assertEqual(len(list(self.config.raw_dir.iterdir())), 1)
            self.assertFalse((self.config.chunks_dir / f"{doc['id']}.json").exists())

            tool_list = self.api.get("/api/tools")
            self.assertEqual(tool_list.status_code, 200, tool_list.text)
            listing = tool_list.json()["tools"]
            self.assertEqual([item["id"] for item in listing], [doc["id"]])
            self.assertFalse(listing[0]["indexed"])
            self.assertEqual(self.api.get("/api/documents").json()["documents"], [])

            rebuilt = self.api.post("/api/documents/rebuild")
            self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
            self.assertEqual(rebuilt.json(), {"chunks": 0, "documents": 0})
            ingest.assert_not_called()
            self.embedder.embed.assert_not_called()
            self.assertEqual(self.vectors.rows, {})

        employee = {"username": "engineer", "role": "user", "department_ids": ["operations"]}
        for collection in ("tools", "documents"):
            with self.subTest(collection=collection):
                url = f"/api/{collection}/{doc['id']}/download"
                with access.identity_scope(employee):
                    downloaded = self.api.get(url)
                self.assertEqual(downloaded.status_code, 200, downloaded.text)
                self.assertEqual(downloaded.content, content)
                self.assertEqual(downloaded.headers["Content-Type"], "application/octet-stream")
                disposition = unquote(downloaded.headers["Content-Disposition"])
                self.assertTrue(disposition.startswith("attachment;"))
                self.assertTrue(disposition.endswith(f"filename*=UTF-8''{name}"))
                with access.identity_scope({**employee, "department_ids": ["finance"]}):
                    self.assertEqual(self.api.get(url).status_code, 404)

    def test_tools_and_documents_have_separate_uploads_and_listings(self):
        for path, fields in (("/api/documents", {}), ("/api/documents", {"doc_type": "record"}),
                             ("/api/knowledge", {})):
            with self.subTest(path=path, fields=fields):
                response = self.upload("驱动.exe", b"installer bytes", path=path, **fields)
                self.assertEqual(response.status_code, 400, response.text)
        reject_document = self.upload_tool("配置说明.md", b"Configuration procedure")
        self.assertEqual(reject_document.status_code, 400, reject_document.text)
        self.assertEqual(self.records, {})
        self.assertEqual(list(self.config.raw_dir.iterdir()), [])

        document = self.upload("配置说明.md", "# 配置说明\n\n检查串口调试参数。".encode())
        operations_tool = self.upload_tool("驱动.exe", b"installer bytes")
        finance_tool = self.upload_tool("财务工具.msi", b"finance installer", department_id="finance")
        for response in (document, operations_tool, finance_tool):
            self.assertEqual(response.status_code, 200, response.text)
        documents = self.api.get("/api/documents", params={"doc_type": "doc"}).json()
        self.assertEqual([item["id"] for item in documents["documents"]], [document.json()["doc"]["id"]])
        self.assertEqual(documents["departments"], [{"id": "operations", "name": "运维部", "count": 1}])
        tools = self.api.get("/api/tools", params={"department_id": "finance"}).json()
        self.assertEqual([item["id"] for item in tools["tools"]], [finance_tool.json()["doc"]["id"]])
        self.assertEqual({item["id"]: item["count"] for item in tools["departments"]},
                         {"operations": 1, "finance": 1})

    def test_tool_download_link_survives_saving_and_preview_in_both_document_types(self):
        installer = self.upload_tool("驱动.exe", b"installer bytes")
        self.assertEqual(installer.status_code, 200, installer.text)
        tool_id = installer.json()["doc"]["id"]
        link = f"[下载驱动.exe](/api/tools/{tool_id}/download)"
        content = f"# 安装驱动\n\n请先{link}。"

        document = self.upload("配置说明.md", "# 配置说明\n\n待补充安装步骤。".encode())
        self.assertEqual(document.status_code, 200, document.text)
        doc_id = document.json()["doc"]["id"]
        updated = self.api.put(f"/api/documents/{doc_id}", json={"content": content})
        created = self.api.post("/api/documents/markdown", json={
            "filename": "驱动安装记录", "content": content, "department_id": "operations",
        })

        for response, doc_type in ((updated, "doc"), (created, "record")):
            with self.subTest(doc_type=doc_type):
                self.assertEqual(response.status_code, 200, response.text)
                saved = response.json()["doc"]
                self.assertEqual(saved["doc_type"], doc_type)
                self.assertEqual(saved["status"], "ready")
                preview = self.api.get(f"/api/documents/{saved['id']}/preview")
                self.assertEqual(preview.status_code, 200, preview.text)
                text = "\n".join(section.get("text", "") for section in preview.json()["sections"])
                self.assertIn(link, text)

    def test_tool_replacement_preserves_download_link_and_indexed_documents(self):
        text_doc = self.upload("配置说明.md", "# 配置说明\n\n串口调试参数为 9600。".encode())
        self.assertEqual(text_doc.status_code, 200, text_doc.text)
        document = text_doc.json()["doc"]
        self.assertTrue(document["indexed"])
        installer = self.upload_tool("驱动.msi", b"installer version 1")
        self.assertEqual(installer.status_code, 200, installer.text)
        tool = installer.json()["doc"]
        self.assertEqual(self.api.get(f"/api/tools/{document['id']}/download").status_code, 404)
        vectors = self.vectors.rows.copy()
        self.assertTrue(vectors)
        self.embedder.reset_mock()

        blocked = self.replace_file("documents", document["id"], "驱动.msi", b"installer other version")
        self.assertEqual(blocked.status_code, 400, blocked.text)
        blocked = self.replace_file("tools", tool["id"], "说明.md", b"Configuration procedure")
        self.assertEqual(blocked.status_code, 400, blocked.text)
        blocked = self.replace_file("tools", document["id"], "驱动.msi", b"installer other version")
        self.assertEqual(blocked.status_code, 404, blocked.text)
        blocked = self.replace_file("documents", tool["id"], "说明.md", b"Configuration procedure")
        self.assertEqual(blocked.status_code, 404, blocked.text)

        with patch.object(self.retrieval, "rebuild", wraps=self.retrieval.rebuild) as rebuild:
            update = self.replace_file("tools", tool["id"], "驱动.7z", b"installer archive version 2")
            self.assertEqual(update.status_code, 200, update.text)
            self.assertFalse(update.json()["doc"]["indexed"])
            self.assertEqual(update.json()["doc"]["id"], tool["id"])
            rebuild.assert_not_called()
        self.embedder.embed.assert_not_called()
        self.assertEqual(self.vectors.rows, vectors)
        self.assertEqual({chunk.doc_id for chunk in self.retrieval.index.chunks}, {document["id"]})
        link = f"/api/tools/{tool['id']}/download"
        self.assertEqual(self.api.get(link).content, b"installer archive version 2")

    def test_tool_permissions_and_delete_enforce_department_access(self):
        upload = self.upload_tool("驱动.exe", b"installer bytes")
        self.assertEqual(upload.status_code, 200, upload.text)
        tool_id = upload.json()["doc"]["id"]
        endpoint = f"/api/tools/{tool_id}"
        download = endpoint + "/download"
        self.assertEqual(self.api.delete(f"/api/documents/{tool_id}").status_code, 404)
        self.assertEqual(self.api.patch(f"/api/documents/{tool_id}/access", json={}).status_code, 404)
        finance = {"username": "finance-reader", "role": "user", "department_ids": ["finance"],
                   "department_roles": {"finance": "editor"}}
        with access.identity_scope(finance):
            self.assertEqual(self.api.get(download).status_code, 404)
        scope = self.api.patch(endpoint + "/access", json={"shared_department_ids": ["finance"]})
        self.assertEqual(scope.status_code, 200, scope.text)
        self.assertEqual(scope.json()["doc"]["shared_department_ids"], ["finance"])
        with access.identity_scope(finance):
            self.assertEqual(self.api.get(download).status_code, 200)
            self.assertEqual(self.api.delete(endpoint).status_code, 403)
            self.assertEqual(self.api.patch(endpoint + "/access", json={"visibility": "all"}).status_code, 403)
        deleted = self.api.delete(endpoint)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted"], tool_id)
        self.assertEqual(self.api.get(download).status_code, 404)
        self.assertEqual(list(self.config.raw_dir.iterdir()), [])

    def test_identical_bytes_cannot_make_a_tool_resolve_to_an_indexed_document(self):
        content = "# 配置说明\n\n串口调试参数为 9600。".encode()
        document = self.upload("配置说明.md", content)
        self.assertEqual(document.status_code, 200, document.text)
        installer = self.upload_tool("驱动.zip", content)
        self.assertEqual(installer.status_code, 200, installer.text)
        first = document.json()["doc"]
        second = installer.json()["doc"]
        self.assertFalse(installer.json()["duplicated"])
        self.assertFalse(second["indexed"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual({chunk.doc_id for chunk in self.retrieval.index.chunks}, {first["id"]})

    def test_tool_limit_is_distinct_and_overflow_retains_the_original_file(self):
        with patch.object(kb, "MAX_TOOL_UPLOAD_BYTES", 4), patch.object(kb, "MAX_UPLOAD_BYTES", 2):
            rejected = self.upload_tool("tool.exe", b"12345")
            self.assertEqual(rejected.status_code, 413, rejected.text)
            self.assertEqual(list(self.config.raw_dir.iterdir()), [])
            self.assertEqual(self.records, {})
            rejected_text = self.upload("document.md", b"1234")
            self.assertEqual(rejected_text.status_code, 413, rejected_text.text)

            accepted = self.upload_tool("tool.exe", b"1234")
            self.assertEqual(accepted.status_code, 200, accepted.text)
            doc = accepted.json()["doc"]
            overflow = self.replace_file("tools", doc["id"], "tool.zip", b"12345")
            self.assertEqual(overflow.status_code, 413, overflow.text)
            self.assertEqual(len(list(self.config.raw_dir.iterdir())), 1)
            self.assertEqual(self.api.get(f"/api/tools/{doc['id']}/download").content, b"1234")

    def test_tool_stream_is_read_in_blocks_and_partial_overflow_is_discarded(self):
        source = io.BytesIO(b"12345")

        def read_piece(size):
            self.assertGreater(size, 0)
            self.assertLessEqual(size, 1024 * 1024)
            return io.BytesIO.read(source, min(size, 2))

        with patch.object(source, "read", side_effect=read_piece) as read:
            with patch.object(kb, "MAX_TOOL_UPLOAD_BYTES", 4):
                with self.assertRaises(HTTPException) as raised:
                    kb.upload_tool("tool.exe", source, department_id="operations")
                self.assertEqual(raised.exception.status_code, 413)
                self.assertEqual(list(self.config.raw_dir.iterdir()), [])
                self.assertEqual(self.records, {})

            source.seek(0)
            read.reset_mock()
            result = kb.upload_tool("tool.exe", source, department_id="operations")
            self.assertEqual(read.call_count, 4)
        self.assertEqual(result["doc"]["size"], 5)
        self.assertEqual(self.api.get(f"/api/tools/{result['doc']['id']}/download").content, b"12345")


if __name__ == "__main__":
    unittest.main()
