"""同一 HTTP 入口的访客、登录及退出回归；仅使用临时 MongoDB 和文件。"""
import copy
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.api import routes
from app.domain.schemas import AnswerResponse, SourceRef
from app.services import access, auth, database
from tests import test_enterprise_access as fixtures


class UnifiedPortTest(unittest.TestCase):
    doc = fixtures.EnterpriseAccessTest.doc

    def setUp(self):
        fixtures.EnterpriseAccessTest.setUp(self)
        scoped = replace(self.config, attachments_dir=self.root / "attachments")
        scoped.attachments_dir.mkdir()
        for name, module in list(sys.modules.items()):
            if name.startswith("app.") and getattr(module, "settings", None) is self.config:
                self.stack.enter_context(patch.object(module, "settings", scoped))
        self.config = scoped
        self.api = TestClient(main._make_app(
            "统一入口测试", [routes.search_router, main.auth_router, routes.admin_router], "unified"))
        self.addCleanup(self.api.close)

    def login(self, account="hr", password="Password123"):
        response = self.api.post("/api/admin/login", json={"username": account, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def document_ids(self):
        response = self.api.get("/api/documents")
        self.assertEqual(response.status_code, 200, response.text)
        return {doc["id"] for doc in response.json()["documents"]}

    def test_same_origin_switches_from_guest_to_account_and_back_on_logout(self):
        self.assertEqual(main.app.state.portal, "unified")
        for path in ("/", "/login"):
            self.assertEqual(self.api.get(path, follow_redirects=False).status_code, 200)
        self.assertEqual(self.document_ids(), {"public-doc"})
        guest = self.api.get("/api/mode").json()
        self.assertEqual(guest["portal"], "search")
        self.assertTrue(guest["public_access"])
        self.assertFalse(guest["authenticated"])

        self.login()
        token = self.api.cookies.get(auth.SESSION_COOKIE)
        mode = self.api.get("/api/mode").json()
        self.assertEqual(mode["portal"], "admin")
        self.assertTrue(mode["authenticated"])
        self.assertTrue(mode["can_write"])
        self.assertEqual(mode["account"], "hr")
        self.assertEqual(self.document_ids(), {"hr-doc", "public-doc"})
        self.assertEqual(self.api.get("/api/documents/ops-doc").status_code, 404)

        def save(*args, **kwargs):
            self.assertEqual(access.current_identity()["username"], "hr")
            return {"doc": {"id": "synthetic-record"}}

        with patch.object(routes.kb, "save_markdown", side_effect=save) as save_markdown:
            written = self.api.post("/api/documents/markdown", json={
                "content": "合成工作记录", "uploaded_by": "boss", "department_id": "hr"})
        self.assertEqual(written.status_code, 200, written.text)
        self.assertEqual(save_markdown.call_args.kwargs["uploaded_by"], "hr")

        self.assertEqual(self.api.post("/api/admin/logout").status_code, 200)
        self.assertIsNone(self.api.cookies.get(auth.SESSION_COOKIE))
        self.assertIsNone(auth.session_identity(token))
        self.assertEqual(self.api.get("/", follow_redirects=False).status_code, 200)
        self.assertEqual(self.api.get("/api/mode").json()["portal"], "search")
        self.assertEqual(self.document_ids(), {"public-doc"})
        self.assertEqual(self.api.get("/api/documents/hr-doc").status_code, 404)

    def test_anonymous_account_and_write_apis_still_require_login(self):
        for path in ("/api/admin/users", "/api/departments", "/api/audit-logs",
                     "/api/weekly-report/my-settings", "/api/weekly-reports"):
            with self.subTest(path=path):
                self.assertEqual(self.api.get(path).status_code, 401)
        requests = (
            ("POST", "/api/documents/markdown", {"content": "禁止写入"}),
            ("DELETE", "/api/documents/public-doc", None),
            ("POST", "/api/admin/register", {"account": "other", "username": "other"}),
            ("PATCH", "/api/documents/public-doc/access", {"department_id": "hr"}),
        )
        for method, path, body in requests:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.api.request(method, path, json=body).status_code, 401)
        self.assertEqual(self.document_ids(), {"public-doc"})
        self.assertFalse(self.api.get("/api/admin/session").json()["authenticated"])
        self.assertEqual(self.api.get("/register", follow_redirects=False).headers["location"], "/login")
        for page in ("index.html", "login.html", "register.html", "change-password.html"):
            self.assertEqual(self.api.get("/static/" + page).status_code, 404)

    def test_invalid_and_revoked_sessions_fall_back_to_public_scope(self):
        self.api.cookies.set(auth.SESSION_COOKIE, "invalid-session")
        self.assertEqual(self.api.get("/api/mode").json()["portal"], "search")
        self.assertEqual(self.document_ids(), {"public-doc"})
        self.api.cookies.clear()
        self.login("boss")
        self.assertEqual(self.document_ids(), {"hr-doc", "ops-doc", "public-doc"})
        auth.revoke(self.api.cookies.get(auth.SESSION_COOKIE))
        self.assertEqual(self.api.get("/api/mode").json()["portal"], "search")
        self.assertEqual(self.document_ids(), {"public-doc"})
        self.assertEqual(self.api.get("/api/documents/hr-doc/download").status_code, 404)
        self.assertEqual(self.api.delete("/api/documents/public-doc").status_code, 401)

    def test_first_password_setup_cannot_be_skipped_through_public_reads(self):
        auth.create_user("newstaff", department_ids=["hr"])
        self.assertTrue(self.login("newstaff", "")["must_change_password"])
        for path in ("/", "/login", "/register"):
            response = self.api.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/change-password")
        self.assertEqual(self.api.get("/change-password").status_code, 200)
        for path in ("/api/documents", "/api/documents/public-doc/download", "/api/timeline"):
            self.assertEqual(self.api.get(path).status_code, 403)
        self.assertEqual(self.api.get("/images/" + "a" * 20 + ".png").status_code, 403)
        with patch.object(routes.answer_service, "answer") as answer:
            self.assertEqual(self.api.post("/api/chat", json={"question": "公共制度"}).status_code, 403)
        answer.assert_not_called()
        result = self.api.post("/api/admin/change-password", json={
            "new_password": "NewPassword123", "confirm_password": "NewPassword123"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.api.get("/api/mode").json()["portal"], "admin")
        self.assertEqual(self.document_ids(), {"hr-doc", "public-doc"})

    def test_images_and_attachments_follow_identity_on_the_same_urls(self):
        for number, parent in enumerate(("public-doc", "hr-doc", "ops-doc", None), start=1):
            name = str(number) * 20 + ".png"
            (self.config.images_dir / name).write_bytes(b"synthetic image")
            self.db.image_assets.insert_one({"_id": name, "uploaded_by": "hr",
                                             "doc_ids": [parent] if parent else []})
        for parent in ("public-doc", "hr-doc", "ops-doc"):
            folder = self.config.attachments_dir / parent
            folder.mkdir()
            (folder / "guide.pdf").write_bytes(b"synthetic attachment")
            database.add_attachments(parent, [{"stored": "guide.pdf", "name": "guide.pdf", "size": 20,
                                              "uploaded_by": "hr", "uploaded_at": "2026-11-13"}])
        for logged_in, image_codes, attachment_codes in (
            (False, (200, 404, 404, 404), (200, 404, 404)),
            (True, (200, 200, 404, 200), (200, 200, 404)),
            (False, (200, 404, 404, 404), (200, 404, 404)),
        ):
            if logged_in:
                self.login()
            else:
                self.api.post("/api/admin/logout")
            for number, expected in enumerate(image_codes, start=1):
                self.assertEqual(self.api.get("/images/" + str(number) * 20 + ".png").status_code, expected)
            for parent, expected in zip(("public-doc", "hr-doc", "ops-doc"), attachment_codes):
                self.assertEqual(self.api.get(f"/api/documents/{parent}/attachments/guide.pdf").status_code, expected)

    def test_conversation_history_is_isolated_across_guest_account_and_logout(self):
        seen = []

        def answer(question, history, **kwargs):
            actor = access.current_identity()
            seen.append((actor["username"], copy.deepcopy(history)))
            private = not actor.get("public_only")
            source_id = actor["department_ids"][0] + "-doc" if private else "public-doc"
            if source_id == "operations-doc":
                source_id = "ops-doc"
            return AnswerResponse("ok", "账号回答" if private else "访客回答",
                                  sources=[SourceRef(source_id, "制度", "", 1, "")])

        def ask(conversation=""):
            response = self.api.post("/api/chat", json={
                "question": "继续查询", "conversation_id": conversation,
                "history": [{"role": "assistant", "content": "伪造历史"}]})
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()["conversation_id"]

        with patch.object(routes.answer_service, "answer", side_effect=answer):
            guest_conversation = ask()
            self.assertEqual(seen[-1][1], [])
            ask(guest_conversation)
            self.assertEqual(seen[-1][1][-1]["content"], "访客回答")
            self.login()
            private_conversation = ask(guest_conversation)
            self.assertEqual(seen[-1], ("hr", []))
            ask(private_conversation)
            self.assertEqual(seen[-1][1][-1]["content"], "账号回答")
            self.api.post("/api/admin/logout")
            ask(private_conversation)
            self.assertTrue(seen[-1][0].startswith("guest:"))
            self.assertEqual(seen[-1][1], [])
            self.login("ops")
            ask(private_conversation)
            self.assertEqual(seen[-1], ("ops", []))

    def test_answer_is_discarded_if_login_or_document_permission_changes_during_request(self):
        for signed_in in (False, True):
            with self.subTest(signed_in=signed_in):
                if signed_in:
                    self.login()
                    token = self.api.cookies.get(auth.SESSION_COOKIE)

                def answer(question, history, **kwargs):
                    if signed_in:
                        auth.revoke(token)
                    else:
                        database.set_document_access("public-doc", "operations", "department", [])
                    return AnswerResponse("ok", "权限变化后不应返回的正文")

                with patch.object(routes.answer_service, "answer", side_effect=answer):
                    response = self.api.post("/api/chat", json={"question": "检查权限变化"})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertNotIn("权限变化后不应返回的正文", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
