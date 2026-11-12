"""账号角色、页面权限和首启初始化回归；不依赖 MongoDB 服务。"""
import copy
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app.api import accounts
from app.config import settings
from app.services import auth


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = replace(settings, index_dir=self.root, auth_enabled=False,
                              auth_admin_user="admin", auth_admin_password="")
        self.rows = {}
        self.users = MagicMock()
        self.users.find_one.side_effect = lambda query, *args: copy.deepcopy(self.rows.get(query["_id"]))
        self.users.find.side_effect = lambda *args, **kwargs: copy.deepcopy(list(self.rows.values()))
        self.users.insert_one.side_effect = self.insert
        self.users.update_one.side_effect = self.update
        self.users.delete_one.side_effect = self.delete
        self.users.count_documents.side_effect = lambda query: len(self.rows)
        patches = [patch.object(auth, "settings", self.config),
                   patch.object(auth.mongo, "db", return_value=SimpleNamespace(users=self.users))]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        auth._sessions.clear()
        self.addCleanup(auth._sessions.clear)
        app = FastAPI()
        app.include_router(accounts.auth_router)
        self.api = TestClient(app)

    def insert(self, document):
        if (document["_id"] in self.rows or any(
                set(document.get("login_names", [])) & set(row.get("login_names", []))
                for row in self.rows.values())):
            raise DuplicateKeyError("duplicate")
        self.rows[document["_id"]] = copy.deepcopy(document)

    def update(self, query, values):
        row = self.rows.get(query["_id"])
        if row is not None and all(row.get(key) == value for key, value in query.items()):
            row.update(values["$set"])
            for key, delta in values.get("$inc", {}).items():
                row[key] = row.get(key, 0) + delta
            return SimpleNamespace(modified_count=1, matched_count=1)
        return SimpleNamespace(modified_count=0, matched_count=0)

    def delete(self, query):
        row = self.rows.get(query["_id"])
        if row and all(row.get(key) == value for key, value in query.items()):
            self.rows.pop(query["_id"])

    def login(self, username="admin", role="admin"):
        auth.create_user(username, "test-password", role=role)
        response = self.api.post("/api/admin/login", json={"username": username, "password": "test-password"})
        self.assertEqual(response.status_code, 200)
        return response

    def test_login_requires_password_even_when_legacy_auth_disabled(self):
        auth.create_user("operator", "real-password")
        for payload in ({}, {"username": "operator", "password": "wrong"}, {"username": "nobody", "password": "wrong"}):
            response = self.api.post("/api/admin/login", json=payload)
            self.assertEqual(response.status_code, 401)
        self.assertFalse(self.api.get("/api/admin/session").json()["authenticated"])

    def test_login_payload_cookie_and_logout(self):
        response = self.login("operator", role="user")
        self.assertEqual(response.json(), {"admin": True, "authenticated": True, "auth": True,
                                          "user": "operator", "account": "operator", "username": "operator",
                                          "display_name": "operator", "role": "user", "can_register": False,
                                          "must_change_password": False})
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        token = self.api.cookies.get(auth.SESSION_COOKIE)
        self.assertTrue(self.api.get("/api/admin/session").json()["authenticated"])
        self.assertFalse(self.api.post("/api/admin/logout").json()["authenticated"])
        self.assertFalse(auth.verify(token))

    def test_only_admin_can_register_and_duplicate_does_not_reset_password(self):
        payload = {"account": "operator", "username": "张三"}
        self.assertEqual(self.api.post("/api/admin/register", json=payload).status_code, 401)
        response = self.login()
        self.assertTrue(response.json()["can_register"])
        registered = self.api.post("/api/admin/register", json=payload)
        self.assertEqual(registered.status_code, 201)
        self.assertEqual(registered.json()["role"], "user")
        duplicate = self.api.post("/api/admin/register", json=payload)
        self.assertEqual(duplicate.status_code, 409)
        self.assertTrue(auth.authenticate("operator", ""))
        self.assertFalse(auth.authenticate("operator", "replacement-password"))
        self.api.post("/api/admin/login", json={"username": "张三"})
        self.assertEqual(self.api.post("/api/admin/register", json={"account": "someone", "username": "李四"}).status_code, 403)

    def test_registration_validates_username_password_and_forbids_admin_role(self):
        self.login()
        for account, username, password, role in (("中文账号", "张三", "", "user"), ("valid", "user name", "", "user"),
                                                  ("valid", "张三", "password", "user"),
                                                  ("valid", "张三", "", "admin"), ("valid", "_wrong", "", "user"),
                                                  ("1abc", "张三", "", "user"), ("a" * 33, "张三", "", "user")):
            with self.subTest(account=account, role=role):
                response = self.api.post("/api/admin/register", json={"account": account, "username": username, "password": password, "role": role})
                self.assertEqual(response.status_code, 400)
        # 1 位与 2 位账号在新规则下合法。
        short = self.api.post("/api/admin/register", json={"account": "a", "username": "单字"})
        self.assertEqual(short.status_code, 201)
        self.assertEqual(set(self.rows), {"admin", "a"})

    def test_identity_refreshes_roles_deletion_and_expiry(self):
        self.login()
        self.rows["admin"]["role"] = "user"
        self.assertFalse(self.api.get("/api/admin/session").json()["can_register"])
        self.rows.pop("admin")
        self.assertFalse(self.api.get("/api/admin/session").json()["authenticated"])
        auth.create_user("operator", "test-password")
        token = auth.create_session("operator")
        auth._sessions[token]["expires"] = time.time() - 1
        self.assertIsNone(auth.session_identity(token))

    def test_legacy_roles_only_promote_configured_admin(self):
        for username in ("admin", "operator"):
            auth.create_user(username, "test-password")
            self.rows[username].pop("role")
        self.assertEqual(auth.session_identity(auth.create_session("admin"))["role"], "admin")
        self.assertEqual(auth.session_identity(auth.create_session("operator"))["role"], "user")
        self.assertEqual(self.rows["admin"]["role"], "admin")
        self.assertEqual(self.rows["operator"]["role"], "user")

    def test_bootstrap_existing_user_keeps_password_and_fixes_role(self):
        auth.create_user("admin", "existing-password")
        auth.ensure_admin_user("admin", "")
        self.assertEqual(self.rows["admin"]["role"], "admin")
        self.assertTrue(auth.authenticate("admin", "existing-password"))
        self.assertFalse((self.root / "bootstrap-admin.txt").exists())
        auth.ensure_admin_user("admin", "another-password")
        self.assertTrue(auth.authenticate("admin", "existing-password"))

    def test_bootstrap_random_password_written_once_and_matches_database(self):
        auth.ensure_admin_user("admin", "")
        path = self.root / "bootstrap-admin.txt"
        content = path.read_text(encoding="utf-8")
        credentials = dict(line.split(": ", 1) for line in content.splitlines()[:2])
        self.assertEqual(credentials["username"], "admin")
        self.assertGreaterEqual(len(credentials["password"]), 24)
        self.assertNotEqual(credentials["password"], "admin123")
        self.assertTrue(auth.authenticate("admin", credentials["password"]))
        auth.ensure_admin_user("admin", "")
        self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertNotIn(credentials["password"], self.rows["admin"]["password_hash"])

    def test_configured_bootstrap_password_does_not_create_plaintext_file(self):
        auth.ensure_admin_user("admin", "configured-password")
        self.assertTrue(auth.authenticate("admin", "configured-password"))
        self.assertFalse((self.root / "bootstrap-admin.txt").exists())

    def test_bootstrap_file_failure_rolls_back_only_new_account(self):
        with patch.object(auth.os, "open", side_effect=PermissionError()):
            with self.assertRaises(RuntimeError):
                auth.ensure_admin_user("admin", "")
        self.assertNotIn("admin", self.rows)

    def test_admin_pages_and_static_html_are_protected_search_is_public(self):
        from app import main

        for name in ("index.html", "login.html", "register.html", "change-password.html", "styles.css", "app.js"):
            (self.root / name).write_text(name, encoding="utf-8")
        config = replace(self.config, web_dir=self.root)
        with patch.object(main, "settings", config), patch.object(main.audit, "record"):
            admin = TestClient(main._make_app("test", [accounts.auth_router, main.search_router], "admin"))
            search = TestClient(main._make_app("test", [main.search_router], "search"))
            self.assertEqual(admin.get("/", follow_redirects=False).headers["location"], "/login")
            self.assertEqual(admin.get("/register", follow_redirects=False).headers["location"], "/login")
            self.assertEqual(admin.get("/login").status_code, 200)
            self.assertEqual(admin.get("/api/documents").status_code, 401)
            self.assertTrue(admin.get("/api/mode").json()["auth"])
            self.assertEqual(admin.get("/static/register.html").status_code, 404)
            self.assertEqual(admin.get("/static/login.html").status_code, 404)
            self.assertEqual(admin.get("/static/index.html").status_code, 404)
            self.assertEqual(admin.get("/static/styles.css").status_code, 200)
            self.assertEqual(search.get("/").status_code, 200)
            self.assertEqual(search.get("/login").status_code, 404)
            self.assertEqual(search.get("/register").status_code, 404)
            self.assertEqual(search.post("/api/admin/register", json={}).status_code, 404)
            self.assertFalse(search.get("/api/mode").json()["auth"])
            auth.create_user("operator", "test-password")
            admin.post("/api/admin/login", json={"username": "operator", "password": "test-password"})
            self.assertEqual(admin.get("/").status_code, 200)
            self.assertEqual(admin.get("/register").status_code, 403)
            operator_mode = admin.get("/api/mode").json()
            # admin 表示进入管理端；菜单权限必须读取单独的 role 字段。
            self.assertTrue(operator_mode["admin"])
            self.assertEqual(operator_mode["role"], "user")
            self.assertFalse(operator_mode["can_register"])
            auth.create_user("admin", "admin-password", role="admin")
            admin.post("/api/admin/login", json={"username": "admin", "password": "admin-password"})
            self.assertEqual(admin.get("/register").status_code, 200)
            admin_mode = admin.get("/api/mode").json()
            self.assertTrue(admin_mode["admin"])
            self.assertEqual(admin_mode["role"], "admin")
            self.assertTrue(admin_mode["can_register"])

    def test_first_login_requires_password_setup_before_business_permissions(self):
        self.login()
        created = self.api.post("/api/admin/register", json={"account": "zhangsan", "username": "张三"})
        self.assertEqual(created.status_code, 201)
        self.assertTrue(self.rows["zhangsan"]["must_change_password"])
        self.assertNotIn("password", self.rows["zhangsan"])
        self.assertNotIn("password", created.json())
        response = self.api.post("/api/admin/login", json={"username": "张三"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["must_change_password"])
        self.assertEqual(response.json()["account"], "zhangsan")
        self.assertEqual(response.json()["display_name"], "张三")
        token = self.api.cookies.get(auth.SESSION_COOKIE)
        self.assertEqual(accounts.require_session(token)["username"], "zhangsan")
        with self.assertRaises(Exception) as raised:
            accounts.require_login(token)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(self.api.post("/api/admin/register", json={"account": "other", "username": "其他"}).status_code, 403)
        changed = self.api.post("/api/admin/change-password", json={"new_password": "first-password", "confirm_password": "first-password"})
        self.assertEqual(changed.status_code, 200)
        self.assertFalse(changed.json()["must_change_password"])
        self.assertIsNone(auth.session_identity(token))
        self.assertNotEqual(self.api.cookies.get(auth.SESSION_COOKIE), token)
        self.assertEqual(accounts.require_login(self.api.cookies.get(auth.SESSION_COOKIE))["username"], "zhangsan")
        self.assertFalse(auth.authenticate("张三", ""))
        self.assertTrue(auth.authenticate("zhangsan", "first-password"))
        self.assertTrue(auth.authenticate("张三", "first-password"))

    def test_new_password_policy_and_confirmation_do_not_unlock_pending_account(self):
        auth.create_user("operator")
        self.api.post("/api/admin/login", json={"username": "operator"})
        for password, confirm in (("", ""), ("short", "short"), ("        ", "        "), ("long-password", "different")):
            response = self.api.post("/api/admin/change-password", json={"new_password": password, "confirm_password": confirm})
            self.assertEqual(response.status_code, 400)
            self.assertTrue(self.rows["operator"]["must_change_password"])
        self.assertTrue(auth.authenticate("operator", ""))

    def test_normal_password_change_verifies_old_password_and_only_updates_self(self):
        self.login()
        auth.create_user("other", "other-password")
        old_token = self.api.cookies.get(auth.SESSION_COOKIE)
        other_admin_token = auth.create_session("admin")
        remote_old_session = copy.deepcopy(auth._sessions[other_admin_token])
        payload = {"current_password": "wrong-password", "new_password": "new-password", "confirm_password": "new-password"}
        self.assertEqual(self.api.post("/api/admin/change-password", json=payload).status_code, 400)
        self.assertTrue(auth.authenticate("admin", "test-password"))
        changed = self.api.post("/api/admin/change-password", json={**payload, "current_password": "test-password", "username": "other"})
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(auth.authenticate("admin", "new-password"))
        self.assertTrue(auth.authenticate("other", "other-password"))
        self.assertIsNone(auth.session_identity(old_token))
        auth._sessions[other_admin_token] = remote_old_session  # 模拟另一进程仍缓存旧 token。
        self.assertIsNone(auth.session_identity(other_admin_token))
        self.assertEqual(self.rows["admin"]["password_version"], 1)

    def test_login_uses_verified_version_snapshot_under_concurrent_password_change(self):
        auth.create_user("operator", "old-password")
        original = auth.authenticate_identity

        def verified_then_changed(username, password):
            identity = original(username, password)
            auth.change_password("operator", "old-password", "new-password", expected_version=0)
            return identity

        with patch.object(auth, "authenticate_identity", side_effect=verified_then_changed):
            response = self.api.post("/api/admin/login", json={"username": "operator", "password": "old-password"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(auth.SESSION_COOKIE, response.cookies)
        self.assertTrue(auth.authenticate("operator", "new-password"))

    def test_password_compare_and_swap_prevents_two_first_setup_requests_overwriting(self):
        auth.create_user("operator")
        original = self.users.update_one.side_effect

        def racing_update(query, values):
            if "$inc" in values:
                self.rows["operator"]["password_version"] = 1
                self.rows["operator"]["password_hash"] = "changed-by-other-request"
            return original(query, values)

        self.users.update_one.side_effect = racing_update
        with self.assertRaises(auth.PasswordChangeConflict):
            auth.change_password("operator", "", "first-password", expected_version=0)
        self.assertEqual(self.rows["operator"]["password_hash"], "changed-by-other-request")

    def test_legacy_empty_password_requires_setup_but_nonempty_password_keeps_access(self):
        auth.create_user("empty")
        auth.create_user("normal", "existing-password")
        for row in self.rows.values():
            row.pop("must_change_password")
            row.pop("password_version")
        response = self.api.post("/api/admin/login", json={"username": "empty"})
        self.assertTrue(response.json()["must_change_password"])
        self.assertTrue(self.rows["empty"]["must_change_password"])
        self.assertTrue(auth.session_identity(self.api.cookies.get(auth.SESSION_COOKIE))["must_change_password"])
        changed = self.api.post("/api/admin/change-password", json={"new_password": "first-password", "confirm_password": "first-password"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.rows["empty"]["password_version"], 1)
        response = self.api.post("/api/admin/login", json={"username": "normal", "password": "existing-password"})
        self.assertFalse(response.json()["must_change_password"])

    def test_account_and_display_name_share_case_insensitive_unique_login_namespace(self):
        self.login()
        first = self.api.post("/api/admin/register", json={"account": "ZhangSan", "username": "张三"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["account"], "zhangsan")
        for account, display_name in (("other", "张三"), ("zhangsan", "李四"), ("someone", "ZHANGSAN")):
            with self.subTest(account=account, display_name=display_name):
                self.assertEqual(self.api.post("/api/admin/register", json={"account": account, "username": display_name}).status_code, 409)
        for alias in ("zhangsan", "ZHANGSAN", "张三"):
            response = self.api.post("/api/admin/login", json={"username": alias})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["user"], "zhangsan")
            self.assertEqual(auth._sessions[self.api.cookies.get(auth.SESSION_COOKIE)]["user"], "zhangsan")

    def test_legacy_ids_still_login_and_cannot_be_claimed_as_new_display_names(self):
        auth.create_user("旧账号", "legacy-password")
        self.rows["旧账号"].pop("login_names")
        self.rows["旧账号"].pop("display_name")
        response = self.api.post("/api/admin/login", json={"username": "旧账号", "password": "legacy-password"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["display_name"], "旧账号")
        self.login()
        claimed = self.api.post("/api/admin/register", json={"account": "other", "username": "旧账号"})
        self.assertEqual(claimed.status_code, 409)

    def test_ambiguous_legacy_aliases_are_rejected_even_with_matching_password(self):
        auth.create_user("first", "same-password", display_name="一号")
        auth.create_user("second", "same-password", display_name="二号")
        self.rows["first"].update({"display_name": "共享名称"})
        self.rows["second"].update({"display_name": "共享名称"})
        for row in self.rows.values():
            row.pop("login_names")
        self.assertEqual(self.api.post("/api/admin/login", json={"username": "共享名称", "password": "same-password"}).status_code, 401)

    def test_legacy_case_sensitive_ids_keep_exact_login_without_ambiguous_aliases(self):
        for account in ("Admin", "admin"):
            document = auth._user_document(account, "legacy-password", "user")
            document.pop("login_names")
            document.pop("display_name")
            self.insert(document)  # 模拟旧规则下已经存在的不同大小写账号。
        for account in ("Admin", "admin"):
            response = self.api.post("/api/admin/login", json={"username": account, "password": "legacy-password"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["account"], account)
        ambiguous = self.api.post("/api/admin/login", json={"username": "aDmIn", "password": "legacy-password"})
        self.assertEqual(ambiguous.status_code, 401)

    def test_only_admin_lists_users_without_secrets(self):
        self.assertEqual(self.api.get("/api/admin/users").status_code, 401)
        self.login("operator", role="user")
        self.assertEqual(self.api.get("/api/admin/users").status_code, 403)
        auth.create_user("admin", "test-password", role="admin")
        self.api.cookies.clear()
        self.api.cookies.set(auth.SESSION_COOKIE, auth.create_session("admin"))
        auth.create_user("zhangsan", display_name="张三")
        listed = self.api.get("/api/admin/users").json()["users"]
        names = {user["username"] for user in listed}
        self.assertEqual(names, {"admin", "operator", "zhangsan"})
        for user in listed:
            self.assertNotIn("password_hash", user)
            self.assertNotIn("salt", user)
        zhangsan = next(user for user in listed if user["username"] == "zhangsan")
        self.assertEqual(zhangsan["display_name"], "张三")
        self.assertEqual(zhangsan["role"], "user")

    def test_user_management_permissions_refresh_with_the_existing_session(self):
        self.login()
        auth.create_user("operator", "operator-password", display_name="操作员")
        token = self.api.cookies.get(auth.SESSION_COOKIE)
        self.assertEqual(self.api.get("/api/admin/users").status_code, 200)

        # 无需重新登录：已打开用户管理页的账号被降权后，全部接口立即拒绝。
        self.rows["admin"]["role"] = "user"
        self.assertTrue(self.api.get("/api/admin/session").json()["authenticated"])
        self.assertFalse(self.api.get("/api/admin/session").json()["can_register"])
        self.assertEqual(self.api.get("/api/admin/users").status_code, 403)
        self.assertEqual(self.api.post("/api/admin/register", json={
            "account": "newuser", "username": "新用户"}).status_code, 403)
        self.assertEqual(self.api.delete("/api/admin/users/operator").status_code, 403)
        self.assertEqual(set(self.rows), {"admin", "operator"})

        self.rows["admin"]["role"] = "admin"
        self.assertEqual(self.api.cookies.get(auth.SESSION_COOKIE), token)
        self.assertEqual(self.api.get("/api/admin/users").status_code, 200)
        self.assertEqual(self.api.post("/api/admin/register", json={
            "account": "newuser", "username": "新用户"}).status_code, 201)
        self.assertEqual(self.api.delete("/api/admin/users/operator").status_code, 200)
        self.assertEqual(set(self.rows), {"admin", "newuser"})

    def test_delete_user_rules_and_session_invalidation(self):
        payload = {"account": "zhangsan", "username": "张三"}
        self.assertEqual(self.api.delete("/api/admin/users/zhangsan").status_code, 401)
        self.login("operator", role="user")
        self.assertEqual(self.api.delete("/api/admin/users/admin").status_code, 403)
        # 切换到管理员会话
        auth.create_user("admin", "test-password", role="admin")
        self.api.cookies.clear()
        self.api.cookies.set(auth.SESSION_COOKIE, auth.create_session("admin"))
        self.api.post("/api/admin/register", json=payload)
        # 普通账号登录，拿到一个有效会话，删除后应立即失效。
        self.api.cookies.clear()
        self.api.post("/api/admin/login", json={"username": "zhangsan"})
        user_token = self.api.cookies.get(auth.SESSION_COOKIE)
        self.assertTrue(auth.verify(user_token))
        self.api.cookies.clear()
        self.api.cookies.set(auth.SESSION_COOKIE, auth.create_session("admin"))
        # 管理员账号不可删除。
        self.assertEqual(self.api.delete("/api/admin/users/admin").status_code, 400)
        # 不存在的账号不可删除。
        self.assertEqual(self.api.delete("/api/admin/users/nobody").status_code, 400)
        deleted = self.api.delete("/api/admin/users/zhangsan")
        self.assertEqual(deleted.status_code, 200)
        self.assertNotIn("zhangsan", self.rows)
        self.assertFalse(auth.verify(user_token))
        # 重复删除：账号已不存在，返回 400。
        self.assertEqual(self.api.delete("/api/admin/users/zhangsan").status_code, 400)


if __name__ == "__main__":
    unittest.main()
