"""个人工作周报的真实 API 权限回归；仅使用 UUID 临时库与临时文件。"""
import unittest
from unittest.mock import patch

from app.services import auth, database, weekly
from tests import test_enterprise_access as fixtures


class PersonalWeeklyApiTest(unittest.TestCase):
    doc = fixtures.EnterpriseAccessTest.doc
    login_as = fixtures.EnterpriseAccessTest.login_as

    def setUp(self):
        fixtures.EnterpriseAccessTest.setUp(self)
        auth.create_user("unassigned", "Password123", department_ids=[])
        self.stack.enter_context(patch.object(weekly, "week_start", return_value="2026-11-09"))
        self.stack.enter_context(patch.object(weekly, "summarize", return_value="本人的工作周报内容"))

    def personal(self):
        response = self.api.get("/api/weekly-report/my-settings")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save_personal(self, enabled=True, weekday=3, time="09:30"):
        return self.api.put("/api/weekly-report/my-settings", json={
            "enabled": enabled, "weekday": weekday, "time": time,
        })

    def record(self, doc_id, owner, department="hr", visibility="department", shared=None):
        self.doc(doc_id, department, visibility)
        self.db.documents.update_one({"_id": doc_id}, {"$set": {
            "doc_type": "record", "uploaded_by": owner, "uploaded_by_name": "同名记录人",
            "uploaded_at": "2026-11-11 09:00:00", "shared_department_ids": shared or [],
            "summary": "2026年11月11日，完成部门资料整理。", "resolved": "resolved",
        }})

    def test_personal_schedule_is_account_scoped_and_does_not_modify_global_settings(self):
        global_settings = weekly.save_settings(True, 4, "16:30", ["boss", "ops"])
        self.login_as("hr")
        defaults = self.personal()
        self.assertEqual(set(defaults), {"enabled", "weekday", "time"})
        self.assertFalse(defaults["enabled"])
        saved = self.save_personal(weekday=2, time="9:5")
        self.assertEqual(saved.status_code, 200, saved.text)
        expected = {"enabled": True, "weekday": 2, "time": "09:05"}
        self.assertEqual(saved.json(), expected)
        self.assertEqual(self.personal(), expected)
        # 查询参数不能将“我的设置”变成另一个账号的设置。
        self.assertEqual(self.api.get("/api/weekly-report/my-settings?account=ops").json(), expected)

        self.login_as("ops")
        self.assertEqual(self.personal(), defaults)
        other = self.save_personal(False, 6, "20:10")
        self.assertEqual(other.status_code, 200, other.text)
        self.login_as("hr")
        self.assertEqual(self.personal(), expected)
        self.assertEqual(weekly.get_settings(), global_settings)

    def test_personal_settings_cannot_select_another_account_or_global_recipient_list(self):
        self.login_as("hr")
        before = self.personal()
        payload = {"enabled": True, "weekday": 2, "time": "09:00"}
        with patch.object(weekly, "save_personal_settings") as save:
            for extra in ({"account": "ops"}, {"username": "ops"}, {"operators": ["boss"]},
                          {"scope": "managed"}):
                with self.subTest(extra=extra):
                    response = self.api.put("/api/weekly-report/my-settings", json={**payload, **extra})
                    self.assertEqual(response.status_code, 422, response.text)
            save.assert_not_called()
        self.assertEqual(self.personal(), before)
        self.login_as("ops")
        self.assertEqual(self.personal(), before)

    def test_invalid_personal_schedule_preserves_the_last_valid_configuration(self):
        global_settings = weekly.save_settings(False, 5, "18:00", ["boss"])
        self.login_as("hr")
        response = self.save_personal(True, 2, "09:45")
        self.assertEqual(response.status_code, 200, response.text)
        before = self.personal()
        for weekday, time in ((0, "09:00"), (8, "09:00"), (2, "25:00"), (2, "09:70"), (2, "abc")):
            with self.subTest(weekday=weekday, time=time):
                response = self.save_personal(True, weekday, time)
                self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.personal(), before)
        self.assertEqual(weekly.get_settings(), global_settings)

    def test_default_and_explicit_self_generation_are_bound_to_every_authenticated_account(self):
        for account in ("hr", "unassigned", "manager", "boss"):
            self.login_as(account)
            for body in (None, {"scope": "self"}):
                with self.subTest(account=account, body=body), \
                        patch.object(weekly, "generate_reports", return_value=[]) as generate:
                    options = {} if body is None else {"json": body}
                    response = self.api.post("/api/weekly-reports/generate?account=ops&operators=boss", **options)
                    self.assertEqual(response.status_code, 200, response.text)
                    generate.assert_called_once_with(operators=[account])

    def test_ordinary_accounts_cannot_generate_managed_reports_or_smuggle_recipients(self):
        with patch.object(weekly, "generate_reports", return_value=[]) as generate:
            for account in ("hr", "unassigned"):
                self.login_as(account)
                response = self.api.post("/api/weekly-reports/generate", json={"scope": "managed"})
                self.assertEqual(response.status_code, 403, response.text)
            self.login_as("hr")
            for payload in ({"operators": ["ops"]}, {"scope": "self", "account": "ops"},
                            {"scope": "self", "person": "boss"}, {"scope": "all"}):
                with self.subTest(payload=payload):
                    response = self.api.post("/api/weekly-reports/generate", json=payload)
                    self.assertEqual(response.status_code, 422, response.text)
            generate.assert_not_called()

    def test_managed_generation_remains_within_the_administrators_department_scope(self):
        self.record("employee-record", "hr")
        self.record("manager-record", "manager")
        self.record("shared-outside-record", "ops", "operations", shared=["hr"])
        self.record("former-member-record", "ops")  # 作者已不属于该部门，不能生成其完整个人周报。
        self.login_as("manager")
        with patch.object(weekly, "generate_reports", return_value=[]) as generate:
            response = self.api.post("/api/weekly-reports/generate", json={"scope": "managed"})
            self.assertEqual(response.status_code, 200, response.text)
            generate.assert_called_once_with(operators=["hr", "manager"])
        self.login_as("boss")
        with patch.object(weekly, "generate_reports", return_value=[]) as generate:
            response = self.api.post("/api/weekly-reports/generate", json={"scope": "managed"})
            self.assertEqual(response.status_code, 200, response.text)
            generate.assert_called_once_with()

    def test_global_schedule_still_requires_platform_admin(self):
        payload = {"enabled": True, "weekday": 2, "time": "10:00", "operators": ["hr"]}
        for account in ("hr", "unassigned", "manager"):
            self.login_as(account)
            self.assertEqual(self.api.get("/api/weekly-report/settings").status_code, 403)
            self.assertEqual(self.api.put("/api/weekly-report/settings", json=payload).status_code, 403)
        self.login_as("boss")
        self.assertEqual(self.api.get("/api/weekly-report/settings").status_code, 200)
        response = self.api.put("/api/weekly-report/settings", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), payload)

    def test_weekly_apis_require_login_and_initial_password_setup_and_are_absent_from_public_port(self):
        requests = [("GET", "/api/weekly-report/my-settings", {}),
                    ("PUT", "/api/weekly-report/my-settings", {"json": {
                        "enabled": False, "weekday": 5, "time": "18:00"}}),
                    ("GET", "/api/weekly-reports", {}),
                    ("POST", "/api/weekly-reports/generate", {})]
        for method, path, options in requests:
            with self.subTest(stage="anonymous", path=path, method=method):
                self.assertEqual(self.api.request(method, path, **options).status_code, 401)
        auth.create_user("first-login", department_ids=[])
        self.login_as("first-login")
        for method, path, options in requests:
            with self.subTest(stage="password", path=path, method=method):
                self.assertEqual(self.api.request(method, path, **options).status_code, 403)
        self.search.cookies.set(auth.SESSION_COOKIE, auth.create_session("boss"))
        for method, path, options in requests:
            with self.subTest(stage="public", path=path, method=method):
                self.assertIn(self.search.request(method, path, **options).status_code, (404, 405))

    def test_account_without_department_can_configure_generate_and_read_its_own_report(self):
        self.record("public-unassigned-record", "unassigned", "operations", "public")
        self.record("old-private-record", "unassigned")
        self.login_as("unassigned")
        session = self.api.get("/api/admin/session").json()
        self.assertFalse(session["can_write"])
        self.assertFalse(session["can_manage_users"])
        self.assertEqual(self.save_personal().status_code, 200)
        self.assertTrue(self.personal()["enabled"])
        response = self.api.post("/api/weekly-reports/generate", json={"scope": "self"})
        self.assertEqual(response.status_code, 200, response.text)
        reports = response.json()["reports"]
        self.assertEqual([report["person"] for report in reports], ["unassigned"])
        self.assertEqual([item["doc_id"] for item in reports[0]["items"]], ["public-unassigned-record"])
        visible = self.api.get("/api/weekly-reports")
        self.assertEqual(visible.status_code, 200, visible.text)
        self.assertEqual([report["owner_account"] for report in visible.json()["reports"]], ["unassigned"])

    def test_personal_generation_and_history_respect_owner_account_and_current_source_permissions(self):
        self.record("own-record", "hr")
        self.record("own-but-hidden-record", "hr", "operations")
        self.record("shared-own-record", "hr", "operations", shared=["hr"])
        self.record("another-authors-record", "ops", "operations", "public")
        self.login_as("hr")
        response = self.api.post("/api/weekly-reports/generate")
        self.assertEqual(response.status_code, 200, response.text)
        reports = response.json()["reports"]
        self.assertEqual([report["person"] for report in reports], ["hr"])
        self.assertEqual({item["doc_id"] for item in reports[0]["items"]}, {"own-record", "shared-own-record"})
        self.login_as("ops")
        response = self.api.post("/api/weekly-reports/generate")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([report["owner_account"] for report in self.api.get("/api/weekly-reports").json()["reports"]], ["ops"])
        self.login_as("hr")
        self.assertEqual([report["owner_account"] for report in self.api.get("/api/weekly-reports").json()["reports"]], ["hr"])
        database.set_document_access("shared-own-record", "operations", "department", [])
        response = self.api.get("/api/weekly-reports")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["reports"], [])


if __name__ == "__main__":
    unittest.main()
