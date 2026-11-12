"""图片提问接口：格式校验与未配置视觉服务时的降级提示。"""
import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import routes
from app.config import settings
from app.main import search_app
from app.services import auth

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class ChatImageTest(unittest.TestCase):
    def setUp(self):
        identity = {"username": "image-test", "display_name": "图片测试员", "role": "user",
                    "department_ids": ["operations"], "department_roles": {"operations": "member"},
                    "must_change_password": False, "password_version": 0}
        session = patch.object(auth, "session_identity", side_effect=lambda token: identity if token == "image-session" else None)
        session.start()
        self.addCleanup(session.stop)
        self.api = TestClient(search_app)
        self.addCleanup(self.api.close)
        self.api.cookies.set(auth.SESSION_COOKIE, "image-session")

    def test_rejects_non_image_file(self):
        api = self.api
        resp = api.post("/api/chat/image", files={"image": ("x.txt", b"hello", "text/plain")})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("图片", resp.json()["detail"])

    def test_prompts_when_vision_not_configured(self):
        tmp = replace(settings, vision_api_key="")
        with patch.object(routes, "settings", tmp):
            api = self.api
            resp = api.post("/api/chat/image", files={"image": ("x.png", _PNG_BYTES, "image/png")})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("未配置", resp.json()["detail"])

    def test_anonymous_image_question_requires_login(self):
        anonymous = TestClient(search_app)
        self.addCleanup(anonymous.close)
        with patch.object(routes.llm, "describe_image") as describe:
            response = anonymous.post("/api/chat/image", files={"image": ("x.png", _PNG_BYTES, "image/png")})
        self.assertEqual(response.status_code, 401)
        describe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
