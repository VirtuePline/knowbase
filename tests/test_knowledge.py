"""常识库：上传固定归类「常识库」、列表过滤与重复上传保持分类。"""
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pymongo import MongoClient
from fastapi.testclient import TestClient

from app.config import settings
from app.domain import taxonomy
from app.domain.taxonomy import normalize_doc_category
from app.main import admin_app
from app.services import auth, mongo, retrieval_service

_MONGO_URI = os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017")


@contextmanager
def temp_workspace():
    """把所有 app.* 模块的 settings 指向临时目录与独立测试库，避免写进真实知识库。"""
    with tempfile.TemporaryDirectory() as tmp, \
            MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000) as mongo_client, \
            ExitStack() as cleanup:
        root = Path(tmp)
        mongo_client.admin.command("ping")
        test_db = f"kbtest_{uuid4().hex[:8]}"
        cleanup.callback(mongo_client.drop_database, test_db)
        tmp_settings = replace(
            settings,
            raw_dir=root / "raw",
            index_dir=root / "index",
            chunks_dir=root / "index" / "chunks",
            index_file=root / "index" / "bm25.json",
            images_dir=root / "images",
            analysis_use_llm=False,
            embedding_enabled=False,
            rerank_enabled=False,
            llm_api_key="",
            vision_api_key="",
            web_search_enabled=False,
            web_search_api_key="",
            mongo_uri=_MONGO_URI,
            mongo_db=test_db,
        )
        for path in (tmp_settings.raw_dir, tmp_settings.chunks_dir, tmp_settings.images_dir):
            path.mkdir(parents=True, exist_ok=True)

        cleanup.enter_context(patch.object(mongo, "client", return_value=mongo_client))
        modules = [module for name, module in list(sys.modules.items())
                   if name.startswith("app.") and module is not None]
        for module in modules:
            if getattr(module, "settings", None) is settings:
                cleanup.enter_context(patch.object(module, "settings", tmp_settings))
        original_retrieval = retrieval_service.retrieval
        isolated_retrieval = retrieval_service.RetrievalService()
        for module in modules:
            if getattr(module, "retrieval", None) is original_retrieval:
                cleanup.enter_context(patch.object(module, "retrieval", isolated_retrieval))

        auth.create_user("knowledge-test", "knowledge-test-password", role="user")
        token = auth.create_session("knowledge-test")
        cleanup.callback(auth.revoke, token)
        api = TestClient(admin_app)
        cleanup.callback(api.close)
        api.cookies.set(auth.SESSION_COOKIE, token)
        yield tmp_settings, api


class TaxonomyTest(unittest.TestCase):
    def test_knowledge_category_is_valid(self):
        self.assertEqual(normalize_doc_category("常识库"), "常识库")
        self.assertIn(taxonomy.KNOWLEDGE_CATEGORY, taxonomy.DOC_CATEGORIES)


class KnowledgeUploadTest(unittest.TestCase):
    def setUp(self):
        self._ws = temp_workspace()
        self.tmp_settings, self.api = self._ws.__enter__()

    def tearDown(self):
        self._ws.__exit__(None, None, None)

    def test_upload_markdown_classified_as_knowledge(self):
        with patch.object(sys.modules["app.services.kb"], "settings", self.tmp_settings):
            api = self.api
            content = "# 电脑台账\n\n剔废机电脑 IP 10.10.1.50，使用人张三，MAC 11-22-33-44-55-66".encode()
            resp = api.post("/api/knowledge", files={"file": ("电脑台账.md", content, "text/markdown")})
            self.assertEqual(resp.status_code, 200)
            doc = resp.json()["doc"]
            self.assertEqual(doc["category"], "常识库")
            self.assertEqual(doc["doc_type"], "doc", "常识库属于基础文档")
            self.assertEqual(doc["status"], "ready")
            self.assertEqual(doc["uploaded_by"], "knowledge-test")
            doc_id = doc["id"]

            rows = api.get("/api/documents?category=常识库").json()["documents"]
            self.assertIn(doc_id, {d["id"] for d in rows})

    def test_duplicate_upload_keeps_knowledge_category(self):
        with patch.object(sys.modules["app.services.kb"], "settings", self.tmp_settings):
            api = self.api
            content = "# 网段规划\n\n产线网段 10.20.0.0/16".encode()
            first = api.post("/api/knowledge", files={"file": ("网段规划.md", content, "text/markdown")})
            self.assertEqual(first.json()["doc"]["category"], "常识库")
            second = api.post("/api/knowledge", files={"file": ("网段规划.md", content, "text/markdown")})
            self.assertTrue(second.json()["duplicated"])
            self.assertEqual(second.json()["doc"]["category"], "常识库")

    def test_regular_upload_keeps_auto_category(self):
        with patch.object(sys.modules["app.services.kb"], "settings", self.tmp_settings):
            api = self.api
            content = "# 贴标机闪退\n\n处理：重启工控机。".encode()
            resp = api.post("/api/documents", files={"file": ("贴标机闪退.md", content, "text/markdown")})
            self.assertEqual(resp.status_code, 200)
            self.assertNotEqual(resp.json()["doc"]["category"], "常识库", "普通上传不应强制常识库")


class DocTypeTest(unittest.TestCase):
    """基础文档 / 运维问题记录分离：上传类型、列表过滤与统计范围。"""

    def setUp(self):
        self._ws = temp_workspace()
        self.tmp_settings, self.api = self._ws.__enter__()

    def tearDown(self):
        self._ws.__exit__(None, None, None)

    def test_upload_defaults_to_doc_type_doc(self):
        with patch.object(sys.modules["app.services.kb"], "settings", self.tmp_settings):
            api = self.api
            resp = api.post("/api/documents", files={"file": ("设备手册.md", "# 设备手册\n\n参数说明".encode(), "text/markdown")})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["doc"]["doc_type"], "doc")

    def test_upload_record_type(self):
        with patch.object(sys.modules["app.services.kb"], "settings", self.tmp_settings):
            api = self.api
            resp = api.post(
                "/api/documents",
                files={"file": ("闪退记录.md", "# 闪退\n\n处理：重启工控机。".encode(), "text/markdown")},
                data={"doc_type": "record"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["doc"]["doc_type"], "record")

    def test_upload_uses_file_mtime_as_uploaded_at(self):
        """上传时间默认取本地文件的修改时间（前端传 lastModified）。"""
        with patch.object(sys.modules["app.services.kb"], "settings", self.tmp_settings):
            api = self.api
            resp = api.post(
                "/api/knowledge",
                files={"file": ("设备手册.md", "# 设备手册\n\n参数说明".encode(), "text/markdown")},
                data={"file_time": "2026-08-15 14:30:22"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["doc"]["uploaded_at"], "2026-08-15 14:30:22")

    def test_record_defaults_to_file_mtime_date(self):
        """运维记录未选问题日期时，记录时间默认取文件修改日期。"""
        with patch.object(sys.modules["app.services.kb"], "settings", self.tmp_settings):
            api = self.api
            resp = api.post(
                "/api/documents",
                files={"file": ("闪退记录.md", "# 闪退\n\n处理：重启工控机。".encode(), "text/markdown")},
                data={"doc_type": "record", "file_time": "2026-08-15 14:30:22"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["doc"]["uploaded_at"], "2026-08-15 14:30:22")

    def test_invalid_file_time_falls_back_to_now(self):
        """无效的文件时间被忽略：回退服务器当前时间。"""
        with patch.object(sys.modules["app.services.kb"], "settings", self.tmp_settings):
            api = self.api
            resp = api.post(
                "/api/knowledge",
                files={"file": ("手册.md", "# 手册\n\n内容".encode(), "text/markdown")},
                data={"file_time": "not-a-date"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertRegex(resp.json()["doc"]["uploaded_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_edit_log_records_editor(self):
        """在线编辑记录修改人并写入溯源日志（创建 / 修改各一条，新在前）。"""
        with patch.object(sys.modules["app.services.kb"], "settings", self.tmp_settings):
            api = self.api
            created = api.post("/api/documents/markdown",
                               json={"content": "# 记录\n\n处理：重启工控机。", "filename": "溯源测试"})
            self.assertEqual(created.status_code, 200)
            doc_id = created.json()["doc"]["id"]
            updated = api.put(f"/api/documents/{doc_id}",
                              json={"content": "# 记录\n\n处理：重启并检查网线。", "filename": "溯源测试"})
            self.assertEqual(updated.status_code, 200)
            doc = updated.json()["doc"]
            self.assertEqual(doc["last_edited_by"], "knowledge-test")
            self.assertTrue(doc["last_edited_at"])
            edits = api.get(f"/api/documents/{doc_id}/edits").json()["edits"]
            self.assertEqual(sorted(e["action"] for e in edits), ["修改", "创建"])
            self.assertTrue(all(e["edited_at"] for e in edits))

    def test_list_documents_filter_by_doc_type(self):
        with patch.object(sys.modules["app.services.kb"], "settings", self.tmp_settings):
            api = self.api
            api.post("/api/documents", files={"file": ("设备手册.md", "# 设备手册\n\n参数".encode(), "text/markdown")})
            kb = sys.modules["app.services.kb"]
            kb.save_markdown("# 闪退\n\n处理：重启。", "", "闪退记录")

            docs = kb.list_documents(doc_type="doc")
            self.assertEqual([d["name"] for d in docs], ["设备手册.md"])
            recs = kb.list_documents(doc_type="record")
            self.assertEqual([d["name"] for d in recs], ["闪退记录"])
            counts = {c["name"]: c["count"] for c in kb.category_counts(doc_type="doc")}
            self.assertEqual(sum(counts.values()), 1, "doc 视角的分类统计不含运维记录")


if __name__ == "__main__":
    unittest.main()
