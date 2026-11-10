"""API 只接受服务端登录身份，客户端不能伪造上传人。无需数据库。"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.accounts import require_login
from app.api import routes
from app.main import admin_app, search_app
from app.services import audit


class RecordIdentityTest(unittest.TestCase):
    def setUp(self):
        self.before = dict(admin_app.dependency_overrides)
        admin_app.dependency_overrides[require_login] = lambda: {"username": "engineer", "role": "user", "display_name": "张工"}
        self.addCleanup(self.restore_overrides)
        audit_patch = patch.object(audit, "record")
        audit_patch.start()
        self.addCleanup(audit_patch.stop)
        self.api = TestClient(admin_app)

    def restore_overrides(self):
        admin_app.dependency_overrides.clear()
        admin_app.dependency_overrides.update(self.before)

    def test_markdown_owner_comes_from_session_and_parent_is_forwarded(self):
        with patch.object(routes.kb, "save_markdown", return_value={"doc": {"id": "child"}}) as save:
            response = self.api.post("/api/documents/markdown", json={
                "content": "# 后续处理\n处理方案：更换网线。", "filename": "追踪记录",
                "uploaded_by": "forged-admin", "parent_id": "parent", "resolved": "improving"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(save.call_args.kwargs["uploaded_by"], "engineer")
        self.assertEqual(save.call_args.kwargs["parent_id"], "parent")
        self.assertEqual(save.call_args.args[5], "improving")

    def test_file_upload_owner_cannot_be_overridden(self):
        with patch.object(routes.kb, "upload", return_value={"doc": {"id": "child"}}) as save:
            response = self.api.post("/api/documents", files={"file": ("record.md", b"record")},
                                     data={"doc_type": "record", "uploaded_by": "forged-admin", "parent_id": "parent"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(save.call_args.kwargs["uploaded_by"], "engineer")
        self.assertEqual(save.call_args.kwargs["parent_id"], "parent")

    def test_operator_name_forwarded_or_defaults_to_display_name(self):
        """运维人用户名：显式传入时按传入值落库；缺省回退当前登录用户名。"""
        with patch.object(routes.kb, "upload", return_value={"doc": {"id": "child"}}) as save:
            response = self.api.post("/api/documents", files={"file": ("record.md", b"record")},
                                     data={"doc_type": "record", "operator_name": " 张工 "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(save.call_args.kwargs["uploaded_by_name"], " 张工 ")
        with patch.object(routes.kb, "upload", return_value={"doc": {"id": "child"}}) as save:
            self.api.post("/api/documents", files={"file": ("record.md", b"record")}, data={"doc_type": "record"})
        self.assertEqual(save.call_args.kwargs["uploaded_by_name"], "张工")

    def test_knowledge_upload_is_attributed(self):
        with patch.object(routes.kb, "upload", return_value={"doc": {"id": "doc"}}) as save:
            response = self.api.post("/api/knowledge", files={"file": ("manual.md", b"manual")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(save.call_args.kwargs["uploaded_by"], "engineer")

    def test_parent_relation_cannot_be_changed_by_edit(self):
        with patch.object(routes.kb, "save_markdown", side_effect=ValueError("父记录不能修改")):
            response = self.api.put("/api/documents/child", json={"content": "x", "parent_id": "other"})
        self.assertEqual(response.status_code, 400)

    def test_deleting_parent_with_followups_returns_conflict(self):
        with patch.object(routes.kb, "delete", side_effect=ValueError("存在追踪记录，请先处理追踪记录")):
            response = self.api.delete("/api/documents/parent")
        self.assertEqual(response.status_code, 409)

    def test_missing_login_rejects_write_even_if_old_setting_disabled(self):
        admin_app.dependency_overrides.pop(require_login)
        response = self.api.post("/api/documents/markdown", json={"content": "x"})
        self.assertEqual(response.status_code, 401)

    def test_query_port_never_exposes_registration_or_upload(self):
        api = TestClient(search_app)
        self.assertNotIn("post", search_app.openapi()["paths"].get("/api/admin/register", {}))
        self.assertIn(api.post("/api/documents/markdown", json={"content": "x"}).status_code, (404, 405))


if __name__ == "__main__":
    unittest.main()
