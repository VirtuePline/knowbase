"""个人计划、定时幂等和生成期间撤权；仅操作 UUID 临时库。"""
import datetime
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.services import access, auth, database, weekly
from tests import test_enterprise_access as fixtures


class PersonalWeeklyServiceTest(unittest.TestCase):
    doc = fixtures.EnterpriseAccessTest.doc
    login_as = fixtures.EnterpriseAccessTest.login_as

    def setUp(self):
        fixtures.EnterpriseAccessTest.setUp(self)
        self.now = datetime.datetime(2026, 11, 13, 12, 1)
        self.stack.enter_context(patch.object(weekly, "summarize", return_value="本人的工作小结"))

    def record(self, doc_id, owner="hr", department="hr"):
        self.doc(doc_id, department)
        self.db.documents.update_one({"_id": doc_id}, {"$set": {
            "doc_type": "record", "uploaded_by": owner, "uploaded_at": "2026-11-11 09:00:00"}})

    def save(self, account="hr", enabled=True, weekday=5, time="12:00"):
        with access.identity_scope(self.actors[account]):
            return weekly.save_personal_settings(account, enabled, weekday, time)

    def test_personal_settings_are_private_and_validation_does_not_change_existing_plan(self):
        self.save()
        before = self.db.weekly_report_settings.find_one({"_id": "hr"})
        with access.identity_scope(self.actors["hr"]):
            self.assertEqual(weekly.get_personal_settings("hr"), {"enabled": True, "weekday": 5, "time": "12:00"})
            for other in ("ops", "boss"):
                with self.assertRaises(HTTPException) as error:
                    weekly.save_personal_settings(other, False, 1, "09:00")
                self.assertEqual(error.exception.status_code, 403)
            for day, time in ((0, "12:00"), (8, "12:00"), (5, "25:00"), (5, "bad:time")):
                with self.subTest(day=day, time=time), self.assertRaises(ValueError):
                    weekly.save_personal_settings("hr", True, day, time)
        self.assertEqual(self.db.weekly_report_settings.find_one({"_id": "hr"}), before)

    def test_enabled_personal_schedule_runs_without_global_schedule_and_uses_current_own_acl(self):
        self.record("mine")
        self.record("colleagues", owner="manager")
        self.record("no-longer-visible", department="operations")
        self.save()
        self.assertTrue(weekly.run_scheduled_once(self.now))
        report = self.db.weekly_reports.find_one({"_id": "2026-11-09:hr"})
        self.assertEqual([item["doc_id"] for item in report["items"]], ["mine"])
        self.assertEqual(report["owner_account_id"], self.db.users.find_one({"_id": "hr"})["account_id"])
        self.assertTrue(access.is_internal())

    def test_personal_schedule_and_repeated_save_do_not_regenerate_within_the_same_week(self):
        self.record("mine")
        self.save()
        with patch.object(weekly, "summarize", return_value="首份周报") as summarize:
            self.assertTrue(weekly.run_scheduled_once(self.now))
            self.save()
            self.assertFalse(weekly.run_scheduled_once(self.now + datetime.timedelta(minutes=2)))
        self.assertEqual(summarize.call_count, 1)
        self.assertEqual(self.db.weekly_report_settings.find_one({"_id": "hr"})["last_generated_week"], "2026-11-09")

    def test_personal_scheduler_preserves_a_current_manually_generated_report(self):
        self.record("mine")
        self.save()
        with access.identity_scope(self.actors["hr"]):
            weekly.generate_reports("2026-11-09", operators=["hr"])
        with patch.object(weekly, "summarize") as summarize:
            self.assertFalse(weekly.run_scheduled_once(self.now))
        summarize.assert_not_called()

    def test_disabled_personal_plan_overrides_global_without_affecting_other_accounts(self):
        weekly.save_settings(True, 5, "12:00", ["hr", "ops", "deleted-user"])
        self.save(enabled=False)
        self.assertTrue(weekly.run_scheduled_once(self.now))
        self.assertEqual({row["owner_account"] for row in self.db.weekly_reports.find({})}, {"ops"})

    def test_new_account_with_same_name_does_not_inherit_the_deleted_accounts_plan(self):
        self.save()
        original_id = self.db.weekly_report_settings.find_one({"_id": "hr"})["account_id"]
        auth.delete_user("hr")
        auth.create_user("hr", "Password123", department_ids=["hr"])
        current = auth._row_identity(self.db.users.find_one({"_id": "hr"}))
        with access.identity_scope(current):
            self.assertFalse(weekly.get_personal_settings("hr")["enabled"])
        self.assertNotEqual(self.db.users.find_one({"_id": "hr"})["account_id"], original_id)
        self.assertFalse(weekly.run_scheduled_once(self.now))
        self.assertEqual(self.db.weekly_reports.count_documents({}), 0)

    def test_personal_plan_survives_password_change_and_legacy_account_id_is_initialized_once(self):
        self.db.users.update_one({"_id": "hr"}, {"$unset": {"account_id": ""}})
        self.actors["hr"] = auth._row_identity(self.db.users.find_one({"_id": "hr"}))
        self.save()
        account_id = self.db.users.find_one({"_id": "hr"})["account_id"]
        auth.change_password("hr", "Password123", "NewPassword123", expected_version=0)
        current = auth._row_identity(self.db.users.find_one({"_id": "hr"}))
        with access.identity_scope(current):
            self.assertTrue(weekly.get_personal_settings("hr")["enabled"])
        self.assertEqual(self.db.users.find_one({"_id": "hr"})["account_id"], account_id)

    def test_old_request_identity_cannot_configure_or_generate_for_a_recreated_account(self):
        stale = self.actors["hr"]
        auth.delete_user("hr")
        auth.create_user("hr", "Password123", department_ids=["hr"])
        with access.identity_scope(stale):
            for operation in (lambda: weekly.get_personal_settings("hr"),
                              lambda: weekly.save_personal_settings("hr", True, 5, "12:00"),
                              lambda: weekly.generate_reports("2026-11-09", operators=["hr"])):
                with self.assertRaises(HTTPException) as error:
                    operation()
                self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.db.weekly_report_settings.count_documents({}), 0)
        self.assertEqual(self.db.weekly_reports.count_documents({}), 0)

    def test_first_login_accounts_are_not_scheduled_until_the_password_is_set(self):
        auth.create_user("newstaff", department_ids=["hr"])
        weekly.save_personal_settings("newstaff", True, 5, "12:00")
        weekly.save_settings(True, 5, "12:00", ["newstaff"])
        self.assertFalse(weekly.run_scheduled_once(self.now))
        self.assertEqual(self.db.weekly_reports.count_documents({}), 0)

    def test_failure_does_not_mark_personal_schedule_finished_and_next_tick_can_retry(self):
        self.record("mine")
        self.save()
        with patch.object(weekly, "generate_reports", side_effect=RuntimeError("temporary failure")):
            self.assertFalse(weekly.run_scheduled_once(self.now))
        self.assertNotIn("last_generated_week", self.db.weekly_report_settings.find_one({"_id": "hr"}))
        self.assertTrue(weekly.run_scheduled_once(self.now + datetime.timedelta(minutes=1)))

    def test_acl_change_during_summary_discards_content_without_saving_a_report(self):
        self.record("mine")

        def revoke(*args):
            database.set_document_access("mine", "operations", "department", [])
            return "撤权后不可返回的旧内容"

        with access.identity_scope(self.actors["hr"]), patch.object(weekly, "summarize", side_effect=revoke):
            with self.assertRaises(HTTPException) as error:
                weekly.generate_reports("2026-11-09", operators=["hr"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.db.weekly_reports.count_documents({}), 0)

    def test_acl_change_during_save_keeps_old_snapshot_hidden_and_returns_no_content(self):
        self.record("mine")
        original_save = database.save_weekly_report

        def revoke_before_save(*args, **kwargs):
            database.set_document_access("mine", "operations", "department", [])
            original_save(*args, **kwargs)

        with access.identity_scope(self.actors["hr"]), patch.object(database, "save_weekly_report", side_effect=revoke_before_save):
            with self.assertRaises(HTTPException) as error:
                weekly.generate_reports("2026-11-09", operators=["hr"])
            self.assertEqual(weekly.visible_reports(), [])
        self.assertEqual(error.exception.status_code, 409)
        stored = self.db.weekly_reports.find_one({})
        self.assertNotEqual(stored["access_revision"], database.access_revision())


if __name__ == "__main__":
    unittest.main()
