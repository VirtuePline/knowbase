import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.config import settings
from app.main import admin_app, search_app
from app.services import audit, auth, mongo
from app.services import retrieval_service


_MONGO_URI = os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017")


class AuditPermissionTest(unittest.TestCase):
    """走实际 Cookie 认证依赖与路由，权限回归不依赖 MongoDB。"""

    def setUp(self):
        identities = {
            "admin-token": {"username": "admin", "role": "admin", "must_change_password": False},
            "user-token": {"username": "operator", "role": "user", "must_change_password": False},
        }
        identity_patch = patch.object(auth, "session_identity", side_effect=identities.get)
        identity_patch.start()
        self.addCleanup(identity_patch.stop)
        guest_patch = patch.object(auth, "public_identity", return_value={
            "username": "guest:test", "role": "guest", "public_only": True,
            "department_ids": [], "department_roles": {}, "must_change_password": False})
        guest_patch.start()
        self.addCleanup(guest_patch.stop)
        self.logs = [{"time": "2026-11-12 10:00:00", "user": "operator", "action": "保存运维记录"}]
        logs_patch = patch.object(audit, "list_recent", return_value=(self.logs, 21))
        self.list_recent = logs_patch.start()
        self.addCleanup(logs_patch.stop)
        self.api = TestClient(admin_app)
        self.addCleanup(self.api.close)

    def test_unauthenticated_and_expired_sessions_cannot_read_logs(self):
        self.assertEqual(self.api.get("/api/audit-logs").status_code, 401)
        self.api.cookies.set(auth.SESSION_COOKIE, "expired-token")
        self.assertEqual(self.api.get("/api/audit-logs").status_code, 401)
        self.list_recent.assert_not_called()

    def test_normal_user_cannot_read_logs(self):
        self.api.cookies.set(auth.SESSION_COOKIE, "user-token")
        response = self.api.get("/api/audit-logs")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("logs", response.json())
        self.list_recent.assert_not_called()

    def test_admin_can_read_paginated_logs(self):
        self.api.cookies.set(auth.SESSION_COOKIE, "admin-token")
        response = self.api.get("/api/audit-logs?limit=10&page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"logs": self.logs, "total": 21, "page": 2, "page_size": 10})
        self.list_recent.assert_called_once_with(10, 2, "desc")
        # 正序切换
        response = self.api.get("/api/audit-logs", params={"limit": 10, "page": 1, "order": "asc"})
        self.assertEqual(response.status_code, 200)
        self.list_recent.assert_called_with(10, 1, "asc")

    def test_search_port_does_not_expose_logs_even_with_admin_cookie(self):
        api = TestClient(search_app)
        self.addCleanup(api.close)
        api.cookies.set(auth.SESSION_COOKIE, "admin-token")
        self.assertEqual(api.get("/api/audit-logs").status_code, 404)
        self.list_recent.assert_not_called()


class AuditActionNameTest(unittest.TestCase):
    def test_known_actions(self):
        cases = [
            ("POST", "/api/documents/markdown", "保存工作记录"),
            ("PUT", "/api/documents/ab12cd34ef56", "编辑工作记录"),
            ("DELETE", "/api/documents/ab12cd34ef56", "删除文档"),
            ("POST", "/api/documents/rebuild", "重建索引"),
            ("PUT", "/api/fields/f_00aabbcc", "修改字段"),
            ("POST", "/api/admin/login", "登录管理端"),
            ("DELETE", "/api/vendors/华智微视", "删除厂商"),
        ]
        for method, path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(audit.action_name(method, path), expected)

    def test_unknown_action_falls_back(self):
        self.assertEqual(audit.action_name("POST", "/api/unknown"), "POST /api/unknown")
        self.assertEqual(audit.action_name("GET", "/api/documents"), "GET /api/documents")


class AuditRecordTest(unittest.TestCase):
    def setUp(self):
        self.client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
        self.client.admin.command("ping")
        self.test_db = f"kbtest_{uuid4().hex[:8]}"
        self._patch = patch.object(mongo, "settings", replace(settings, mongo_uri=_MONGO_URI, mongo_db=self.test_db))
        self._patch.start()
        self._client_patch = patch.object(mongo, "client", return_value=self.client)
        self._client_patch.start()

    def tearDown(self):
        self._client_patch.stop()
        self._patch.stop()
        self.client.drop_database(self.test_db)
        self.client.close()

    def test_record_stores_ip_time_action(self):
        audit.record("10.1.2.3", "POST", "/api/documents/markdown", 200, detail="name=记录.md")
        row = mongo.db().audit_logs.find_one({}, {"_id": 0})
        self.assertEqual(row["ip"], "10.1.2.3")
        self.assertEqual(row["action"], "保存工作记录")
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["detail"], "name=记录.md")
        self.assertEqual(row["user"], "-")
        self.assertRegex(row["time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_record_failure_never_raises(self):
        with patch.object(mongo, "db", side_effect=RuntimeError("mongo down")):
            audit.record("1.2.3.4", "POST", "/api/documents", 200)   # 不应抛异常

    def test_detail_from_body(self):
        import json
        body = json.dumps({"name": "台账.xlsx", "content": "x"}).encode()
        self.assertEqual(audit.detail_from_body(body, "application/json"), "name=台账.xlsx")
        content_only = json.dumps({"content": "正文内容"}).encode()
        self.assertEqual(audit.detail_from_body(content_only, "application/json"), "content=正文内容")
        self.assertEqual(audit.detail_from_body(b"binary", "multipart/form-data"), "")
        self.assertEqual(audit.detail_from_body(b"", "application/json"), "")


class AuditMiddlewareTest(unittest.TestCase):
    """通过 TestClient 验证：管理端写请求自动落审计日志，GET 不记。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
        self.addCleanup(self.client.close)
        self.client.admin.command("ping")
        self.test_db = f"kbtest_{uuid4().hex[:8]}"
        self.addCleanup(self.client.drop_database, self.test_db)
        self.tmp_settings = replace(
            settings,
            raw_dir=root / "raw",
            index_dir=root / "index",
            chunks_dir=root / "index" / "chunks",
            index_file=root / "index" / "bm25.json",
            images_dir=root / "images",
            mongo_uri=_MONGO_URI,
            mongo_db=self.test_db,
            auth_enabled=False,
            analysis_use_llm=False,
            embedding_enabled=False,
            rerank_enabled=False,
            llm_api_key="",
            vision_api_key="",
        )
        for directory in (self.tmp_settings.raw_dir, self.tmp_settings.chunks_dir,
                          self.tmp_settings.images_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self._patches = ExitStack()
        self.addCleanup(self._patches.close)
        self._patches.enter_context(patch.object(mongo, "client", return_value=self.client))
        modules = [module for name, module in list(sys.modules.items())
                   if name.startswith("app.") and module is not None]
        for module in modules:
            if getattr(module, "settings", None) is settings:
                self._patches.enter_context(patch.object(module, "settings", self.tmp_settings))

        # 每个测试使用独立检索实例，既不读写真实索引，也不污染进程级单例状态。
        original_retrieval = retrieval_service.retrieval
        self.retrieval = retrieval_service.RetrievalService()
        for module in modules:
            if getattr(module, "retrieval", None) is original_retrieval:
                self._patches.enter_context(patch.object(module, "retrieval", self.retrieval))

        # 路径与服务配置的前置断言，防止后续测试误用真实知识库。
        for name in ("app.services.kb", "app.core.ingest.pipeline", "app.services.mongo",
                     "app.services.retrieval_service", "app.core.llm.client",
                     "app.core.retrieval.embeddings", "app.core.retrieval.reranker"):
            self.assertIs(sys.modules[name].settings, self.tmp_settings)
        for path in (self.tmp_settings.raw_dir, self.tmp_settings.index_dir,
                     self.tmp_settings.chunks_dir, self.tmp_settings.index_file,
                     self.tmp_settings.images_dir):
            self.assertTrue(path.resolve().is_relative_to(root.resolve()))
        auth.create_user("auditor", "audit-test-password")
        token = auth.create_session("auditor")
        self.addCleanup(auth.revoke, token)
        self.api = TestClient(admin_app)
        self.api.cookies.set(auth.SESSION_COOKIE, token)

    def test_write_request_is_logged(self):
        # 不进入 with 块即不触发 lifespan（不加载检索模型）
        api = self.api
        resp = api.post("/api/documents/markdown",
                        json={"content": "# 审计验证\n\n内容", "name": "审计验证.md"},
                        headers={"x-forwarded-for": "10.20.30.40"})
        self.assertEqual(resp.status_code, 200)
        doc_id = resp.json()["doc"]["id"]
        self.assertEqual(resp.json()["doc"]["status"], "ready")
        self.assertTrue(list(self.tmp_settings.raw_dir.glob(f"{doc_id}_*")))
        self.assertTrue((self.tmp_settings.chunks_dir / f"{doc_id}.json").is_file())
        self.assertTrue(self.tmp_settings.index_file.is_file())
        self.assertEqual(self.retrieval.index.doc_count(), 1)
        try:
            rows = list(mongo.db().audit_logs.find({}))
            self.assertEqual(len(rows), 1, "仅写请求记日志")
            row = rows[0]
            self.assertEqual(row["ip"], "10.20.30.40", "取 X-Forwarded-For")
            self.assertEqual(row["action"], "保存工作记录")
            self.assertEqual(row["detail"], "name=审计验证.md")
            self.assertEqual(row["status"], 200)
            self.assertEqual(row["user"], "auditor")
        finally:
            api.delete(f"/api/documents/{doc_id}")

        rows = list(mongo.db().audit_logs.find({}))
        self.assertEqual(len(rows), 2, "删除操作也记录")
        self.assertEqual(rows[-1]["action"], "删除文档")   # 插入序：删除在后
        # 普通账号写操作仍被记录，但只有管理员可查询审计日志。
        self.assertEqual(api.get("/api/audit-logs?limit=10").status_code, 403)
        auth.create_user("audit-admin", "audit-admin-password", role="admin")
        token = auth.create_session("audit-admin")
        self.addCleanup(auth.revoke, token)
        api.cookies.set(auth.SESSION_COOKIE, token)
        response = api.get("/api/audit-logs?limit=10")
        self.assertEqual(response.status_code, 200)
        logs = response.json()["logs"]
        self.assertEqual(len(logs), 2)
        self.assertNotIn("_id", logs[0])

    def test_get_request_not_logged(self):
        api = self.api
        api.get("/api/documents")
        self.assertEqual(mongo.db().audit_logs.count_documents({}), 0, "GET 不记日志")


if __name__ == "__main__":
    unittest.main()
