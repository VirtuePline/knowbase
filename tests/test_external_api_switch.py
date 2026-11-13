"""外部 API 总开关的接口权限与离线入口；仅使用 UUID 临时库和合成资料。"""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.api import routes
from app.domain.schemas import AnswerResponse, SourceRef
from app.services import auth, database
from tests import test_enterprise_access as fixtures


_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class ExternalApiSwitchTest(unittest.TestCase):
    doc = fixtures.EnterpriseAccessTest.doc
    login_as = fixtures.EnterpriseAccessTest.login_as

    def setUp(self):
        fixtures.EnterpriseAccessTest.setUp(self)

    def change(self, enabled, client=None):
        response = (client or self.api).post("/api/external-apis", json={"enabled": enabled})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"enabled": enabled})
        return response.json()

    def status(self, client=None):
        response = (client or self.api).get("/api/external-apis")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_default_is_enabled_and_reading_the_switch_does_not_create_a_setting(self):
        self.login_as("hr")
        self.assertIsNone(self.db.app_settings.find_one({"_id": "external_apis"}))
        self.assertTrue(database.external_apis_enabled())
        self.assertEqual(self.status(), {"enabled": True})
        self.assertTrue(self.api.get("/api/mode").json()["external_apis_enabled"])
        self.assertIsNone(self.db.app_settings.find_one({"_id": "external_apis"}))

    def test_only_platform_administrators_can_change_the_switch(self):
        self.login_as("boss")
        self.change(False)
        original = self.db.app_settings.find_one({"_id": "external_apis"})
        for account in ("hr", "manager"):
            with self.subTest(account=account):
                self.login_as(account)
                self.assertEqual(self.status(), {"enabled": False})
                response = self.api.post("/api/external-apis", json={"enabled": True})
                self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.db.app_settings.find_one({"_id": "external_apis"}), original)
        self.login_as("boss")
        self.change(True)
        self.assertEqual(self.db.app_settings.find_one({"_id": "external_apis"})["value"], "on")

    def test_saved_state_is_immediately_visible_to_an_independent_client_and_both_portals(self):
        reader = TestClient(self.api.app)
        self.addCleanup(reader.close)
        self.login_as("reader", client=reader)
        self.login_as("boss")
        self.assertEqual(self.status(reader), {"enabled": True})
        for enabled, stored in ((False, "off"), (True, "on"), (False, "off")):
            with self.subTest(enabled=enabled):
                self.change(enabled)
                self.assertEqual(self.db.app_settings.find_one({"_id": "external_apis"})["value"], stored)
                self.assertEqual(self.status(reader), {"enabled": enabled})
                self.assertEqual(reader.get("/api/mode").json()["external_apis_enabled"], enabled)
                public_mode = self.search.get("/api/mode").json()
                self.assertEqual(public_mode["external_apis_enabled"], enabled)
                self.assertTrue(public_mode["public_access"])
                self.assertFalse(public_mode["authenticated"])
                self.assertEqual(public_mode["role"], "guest")
                self.assertEqual(public_mode["department_ids"], [])
                self.assertEqual(public_mode["departments"], [])

    def test_management_requires_login_and_first_password_setup(self):
        self.assertEqual(self.api.get("/api/external-apis").status_code, 401)
        self.assertEqual(self.api.post("/api/external-apis", json={"enabled": False}).status_code, 401)
        self.assertIsNone(self.db.app_settings.find_one({"_id": "external_apis"}))
        auth.create_user("first-login", department_ids=["hr"])
        self.login_as("first-login")
        self.assertEqual(self.api.get("/api/external-apis").status_code, 403)
        self.assertEqual(self.api.post("/api/external-apis", json={"enabled": False}).status_code, 403)
        self.assertIsNone(self.db.app_settings.find_one({"_id": "external_apis"}))

    def test_public_port_exposes_status_in_mode_but_never_the_management_endpoint(self):
        self.login_as("boss")
        self.change(False)
        self.search.cookies.set(auth.SESSION_COOKIE, auth.create_session("boss"))
        self.assertIn(self.search.get("/api/external-apis").status_code, (404, 405))
        self.assertIn(self.search.post("/api/external-apis", json={"enabled": True}).status_code, (404, 405))
        self.assertFalse(database.external_apis_enabled())
        public_mode = self.search.get("/api/mode").json()
        self.assertFalse(public_mode["external_apis_enabled"])
        self.assertTrue(public_mode["public_access"])
        self.assertFalse(public_mode["admin"])
        self.assertFalse(public_mode["can_manage_users"])
        self.assertEqual(public_mode["account"], "")
        self.assertEqual(public_mode["department_ids"], [])
        self.assertEqual(public_mode["department_roles"], {})
        self.assertEqual(public_mode["departments"], [])

    def test_offline_image_questions_are_blocked_even_when_visual_api_is_configured(self):
        self.login_as("boss")
        self.change(False)
        self.login_as("hr")
        configured = replace(self.config, vision_api_key="configured-test-key")
        with patch.object(routes, "settings", configured), \
                patch.object(routes.llm, "describe_image") as describe:
            for client in (self.api, self.search):
                with self.subTest(portal=client.app.state.portal):
                    response = client.post("/api/chat/image", files={"image": ("sample.png", _PNG, "image/png")})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("离线", response.json()["detail"])
            describe.assert_not_called()

    def test_reenabling_the_switch_restores_image_questions_without_a_restart(self):
        self.login_as("boss")
        self.change(False)
        configured = replace(self.config, vision_api_key="configured-test-key")
        with patch.object(routes, "settings", configured), \
                patch.object(routes.llm, "describe_image", return_value="合成图片中的问题") as describe:
            blocked = self.search.post("/api/chat/image", files={"image": ("sample.png", _PNG, "image/png")})
            self.assertEqual(blocked.status_code, 400, blocked.text)
            describe.assert_not_called()
            self.change(True)
            allowed = self.search.post("/api/chat/image", files={"image": ("sample.png", _PNG, "image/png")})
            self.assertEqual(allowed.status_code, 200, allowed.text)
            self.assertEqual(allowed.json(), {"question": "合成图片中的问题"})
            describe.assert_called_once()

    def test_offline_text_chat_remains_available_for_employees_and_public_visitors(self):
        self.login_as("boss")
        self.change(False)
        self.login_as("hr")
        result = AnswerResponse("ok", "已有文档中的合成答案", sources=[
            SourceRef("public-doc", "公共资料", "", 1.0, "已有文档内容")])
        with patch.object(routes.answer_service, "answer", return_value=result) as answer:
            for client in (self.api, self.search):
                with self.subTest(portal=client.app.state.portal):
                    response = client.post("/api/chat", json={
                        "question": "如何查询已有资料", "llm_polish": True, "web_search": True,
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["answer"], result.answer)
                    self.assertEqual(response.json()["sources"][0]["doc_id"], "public-doc")
            self.assertEqual(answer.call_count, 2)
        self.assertFalse(database.external_apis_enabled())


class ExternalApiFailurePolicyTest(unittest.TestCase):
    def test_unreadable_persisted_policy_fails_closed_without_using_a_default_on_fallback(self):
        with patch.object(database.mongo, "db", side_effect=RuntimeError("合成数据库不可用")):
            self.assertFalse(database.external_apis_enabled())
        settings_collection = Mock()
        settings_collection.find_one.side_effect = RuntimeError("合成设置查询失败")
        with patch.object(database.mongo, "db", return_value=SimpleNamespace(app_settings=settings_collection)):
            self.assertFalse(database.external_apis_enabled())


if __name__ == "__main__":
    unittest.main()
