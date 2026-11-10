"""图片提问接口：格式校验与未配置视觉服务时的降级提示。"""
import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import routes
from app.config import settings
from app.main import search_app

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class ChatImageTest(unittest.TestCase):
    def test_rejects_non_image_file(self):
        api = TestClient(search_app)
        resp = api.post("/api/chat/image", files={"image": ("x.txt", b"hello", "text/plain")})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("图片", resp.json()["detail"])

    def test_prompts_when_vision_not_configured(self):
        tmp = replace(settings, vision_api_key="")
        with patch.object(routes, "settings", tmp):
            api = TestClient(search_app)
            resp = api.post("/api/chat/image", files={"image": ("x.png", _PNG_BYTES, "image/png")})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("未配置", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
