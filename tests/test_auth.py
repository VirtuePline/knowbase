import os
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from pymongo import MongoClient

from app.api import routes as routes_mod
from app.config import settings
from app.api.routes import require_admin
from app.services import auth, mongo


_MONGO_URI = os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017")


def _test_env():
    """独立测试库 + 基础配置，用完即删。"""
    client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    test_db = f"kbtest_{uuid4().hex[:8]}"
    return client, test_db, replace(settings, mongo_uri=_MONGO_URI, mongo_db=test_db)


class AuthAccountTest(unittest.TestCase):
    def setUp(self):
        self.client, self.test_db, self.tmp_settings = _test_env()
        self._patch = patch.object(mongo, "settings", self.tmp_settings)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.client.drop_database(self.test_db)
        self.client.close()

    def test_create_and_authenticate(self):
        auth.create_user("admin", "s3cret")
        self.assertTrue(auth.authenticate("admin", "s3cret"))
        self.assertFalse(auth.authenticate("admin", "wrong"))
        self.assertFalse(auth.authenticate("nobody", "s3cret"))

    def test_password_not_stored_in_plain(self):
        auth.create_user("admin", "s3cret")
        row = mongo.db().users.find_one({"_id": "admin"})
        self.assertNotIn("s3cret", row["password_hash"])
        self.assertEqual(len(row["salt"]), 32)   # 16 字节盐的 hex
        auth.create_user("admin2", "s3cret")
        other = mongo.db().users.find_one({"_id": "admin2"})
        self.assertNotEqual(row["password_hash"], other["password_hash"], "同密码不同盐")

    def test_ensure_admin_does_not_overwrite(self):
        auth.create_user("admin", "real-password")
        auth.ensure_admin_user("admin", "weaker-default")
        self.assertTrue(auth.authenticate("admin", "real-password"))
        self.assertFalse(auth.authenticate("admin", "weaker-default"))

    def test_ensure_admin_creates_when_absent(self):
        self.assertEqual(auth.count_users(), 0)
        auth.ensure_admin_user("admin", "boot-strapped")
        self.assertTrue(auth.authenticate("admin", "boot-strapped"))
        self.assertEqual(auth.count_users(), 1)

    def test_session_lifecycle(self):
        auth.create_user("admin", "test-password")
        token = auth.create_session("admin")
        self.assertTrue(auth.verify(token))
        auth.revoke(token)
        self.assertFalse(auth.verify(token))
        self.assertFalse(auth.verify(None))
        self.assertFalse(auth.verify("bogus-token"))

    def test_session_expires(self):
        token = auth.create_session("admin")
        auth._sessions[token]["expires"] = time.time() - 1   # 人为过期
        self.assertFalse(auth.verify(token))
        self.assertNotIn(token, auth._sessions, "过期会话被移除")


class RequireAdminGateTest(unittest.TestCase):
    """兼容 require_admin 名称，但管理端始终要求真实账号会话。"""

    def setUp(self):
        self.client, self.test_db, self.tmp_settings = _test_env()
        self._patches = [
            patch.object(mongo, "settings", self.tmp_settings),
            patch.object(routes_mod, "settings", self.tmp_settings),   # require_admin 读 routes 的 settings
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.client.drop_database(self.test_db)
        self.client.close()

    def test_disabled_auth_still_rejects_without_session(self):
        with patch.object(routes_mod, "settings", replace(self.tmp_settings, auth_enabled=False)):
            with self.assertRaises(HTTPException) as ctx:
                require_admin(None)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_enabled_auth_rejects_without_session(self):
        with patch.object(routes_mod, "settings", replace(self.tmp_settings, auth_enabled=True)):
            with self.assertRaises(HTTPException) as ctx:
                require_admin(None)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_enabled_auth_accepts_valid_session(self):
        auth.create_user("admin", "test-password")
        token = auth.create_session("admin")
        with patch.object(routes_mod, "settings", replace(self.tmp_settings, auth_enabled=True)):
            self.assertEqual(require_admin(token), {"username": "admin", "role": "user", "display_name": "admin",
                                                    "must_change_password": False, "password_version": 0})


if __name__ == "__main__":
    unittest.main()
