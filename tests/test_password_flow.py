"""真实临时 MongoDB 中验证首次设置密码与会话撤销，不读写业务文件。"""
import copy
import os
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main
from app.api import accounts, routes
from app.config import settings
from app.services import auth, mongo


class PasswordFlowTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.client = MongoClient(os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017"),
                                  serverSelectionTimeoutMS=5000)
        self.addCleanup(self.client.close)
        self.client.admin.command("ping")
        self.db_name = "kbtest_password_" + uuid4().hex[:8]
        self.addCleanup(self.client.drop_database, self.db_name)
        self.db = self.client[self.db_name]
        self.stack.enter_context(patch.object(mongo, "db", return_value=self.db))
        config = replace(settings, web_dir=self.root, index_dir=self.root, auth_admin_user="admin")
        self.stack.enter_context(patch.object(main, "settings", config))
        self.stack.enter_context(patch.object(auth, "settings", config))
        self.audit = self.stack.enter_context(patch.object(main.audit, "record"))
        for page in ("index.html", "login.html", "register.html", "change-password.html"):
            (self.root / page).write_text(page, encoding="utf-8")
        self.sessions_before = copy.deepcopy(auth._sessions)
        self.addCleanup(self.restore_sessions)
        self.app = main._make_app("password test", [accounts.auth_router, routes.admin_router, routes.search_router], "admin")
        self.api = TestClient(self.app)  # 不触发生命周期，不加载真实索引或模型。
        self.addCleanup(self.api.close)
        auth.create_user("admin", "admin-original-password", role="admin")

    def restore_sessions(self):
        auth._sessions.clear()
        auth._sessions.update(self.sessions_before)

    def admin_login(self, api=None):
        response = (api or self.api).post("/api/admin/login", json={"username": "admin", "password": "admin-original-password"})
        self.assertEqual(response.status_code, 200)

    def register_chinese_account(self):
        self.admin_login()
        response = self.api.post("/api/admin/register", json={"account": "zhangsan", "username": "张三"})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(auth.authenticate("张三", ""))
        self.assertTrue(self.db.users.find_one({"_id": "zhangsan"})["must_change_password"])
        self.assertNotIn("password", response.json())

    def newcomer(self):
        self.register_chinese_account()
        api = TestClient(self.app)
        self.addCleanup(api.close)
        response = api.post("/api/admin/login", json={"username": "张三"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["must_change_password"])
        self.assertFalse(response.json()["can_register"])
        return api

    def test_pending_account_cannot_bypass_setup_using_pages_or_business_api(self):
        api = self.newcomer()
        for path in ("/", "/login", "/register"):
            response = api.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/change-password")
        self.assertEqual(api.get("/change-password").status_code, 200)
        self.assertEqual(api.get("/static/change-password.html").status_code, 404)
        self.assertEqual(api.get("/api/documents").status_code, 403)
        self.assertEqual(api.post("/api/documents/markdown", json={"content": "x"}).status_code, 403)
        self.assertEqual(api.post("/api/images", files={"file": ("x.png", b"png")}).status_code, 403)
        self.assertEqual(api.post("/api/admin/register", json={"username": "other"}).status_code, 403)

    def test_first_setup_rotates_session_and_binds_uploads_to_chinese_account(self):
        api = self.newcomer()
        old_token = api.cookies.get(auth.SESSION_COOKIE)
        response = api.post("/api/admin/change-password", json={
            "new_password": "new-user-password", "confirm_password": "new-user-password"})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(api.cookies.get(auth.SESSION_COOKIE), old_token)
        self.assertIsNone(auth.session_identity(old_token))
        self.assertFalse(api.get("/api/admin/session").json()["must_change_password"])
        self.assertEqual(api.get("/").status_code, 200)
        self.assertTrue(auth.authenticate("张三", "new-user-password"))
        self.assertFalse(auth.authenticate("张三", ""))
        self.assertEqual(api.get("/register").status_code, 403)
        with patch.object(routes.kb, "save_markdown", return_value={"doc": {"id": "test"}}) as save:
            result = api.post("/api/documents/markdown", json={"content": "记录内容", "uploaded_by": "admin"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(save.call_args.kwargs["uploaded_by"], "zhangsan")
        self.assertEqual(api.post("/api/admin/login", json={"username": "张三", "password": ""}).status_code, 401)
        self.assertEqual(api.post("/api/admin/login", json={"username": "张三", "password": "new-user-password"}).status_code, 200)
        account_login = api.post("/api/admin/login", json={"username": "ZHANGSAN", "password": "new-user-password"})
        self.assertEqual(account_login.status_code, 200)
        self.assertEqual(account_login.json()["account"], "zhangsan")
        self.assertEqual(account_login.json()["display_name"], "张三")

    def test_login_names_cannot_collide_across_account_and_display_name(self):
        self.register_chinese_account()
        for payload in ({"account": "someone", "username": "ZHANGSAN"},
                        {"account": "zhangsan", "username": "另一个人"},
                        {"account": "different", "username": "张三"}):
            with self.subTest(account=payload["account"]):
                self.assertEqual(self.api.post("/api/admin/register", json=payload).status_code, 409)
        self.assertEqual(self.db.users.count_documents({}), 2)
        self.assertTrue(auth.authenticate("张三", ""))
        rejected = self.api.post("/api/admin/register", json={
            "account": "operator", "username": "操作员", "password": "chosen-by-admin"})
        self.assertEqual(rejected.status_code, 400)
        self.assertIsNone(self.db.users.find_one({"_id": "operator"}))

    def test_invalid_first_password_does_not_unlock_account(self):
        api = self.newcomer()
        before = self.db.users.find_one({"_id": "zhangsan"})["password_hash"]
        for password, confirmation in (("", ""), ("short", "short"), ("        ", "        "),
                                       ("valid-password", "different-password")):
            with self.subTest(password_length=len(password)):
                response = api.post("/api/admin/change-password", json={
                    "new_password": password, "confirm_password": confirmation})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.db.users.find_one({"_id": "zhangsan"})["password_hash"], before)
                self.assertTrue(api.get("/api/admin/session").json()["must_change_password"])

    def test_admin_changes_only_self_and_old_versions_fail_across_processes(self):
        self.register_chinese_account()
        other = TestClient(self.app)
        self.addCleanup(other.close)
        self.admin_login(other)
        old_token = other.cookies.get(auth.SESSION_COOKIE)
        old_session = copy.deepcopy(auth._sessions[old_token])
        wrong = self.api.post("/api/admin/change-password", json={
            "current_password": "wrong", "new_password": "admin-new-password", "confirm_password": "admin-new-password"})
        self.assertIn(wrong.status_code, (400, 401))
        self.assertTrue(auth.authenticate("admin", "admin-original-password"))
        changed = self.api.post("/api/admin/change-password", json={
            "username": "张三", "current_password": "admin-original-password",
            "new_password": "admin-new-password", "confirm_password": "admin-new-password",
            "content": "admin-new-password"})
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(auth.authenticate("admin", "admin-new-password"))
        self.assertFalse(auth.authenticate("admin", "admin-original-password"))
        self.assertTrue(auth.authenticate("张三", ""))
        self.assertFalse(other.get("/api/admin/session").json()["authenticated"])
        # 模拟其他进程仍保留旧 token：仅清内存会话不足以通过此断言。
        auth._sessions[old_token] = old_session
        self.assertIsNone(auth.session_identity(old_token))
        for call in self.audit.call_args_list:
            self.assertNotIn("admin-new-password", str(call.kwargs))
            self.assertNotIn("admin-original-password", str(call.kwargs))
        successful_change = [call.kwargs for call in self.audit.call_args_list
                             if call.kwargs.get("path") == "/api/admin/change-password"
                             and call.kwargs.get("status") == 200]
        self.assertEqual(successful_change[-1]["user"], "admin")
        anonymous = TestClient(self.app)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.post("/api/admin/change-password", json={
            "new_password": "something-new", "confirm_password": "something-new"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
