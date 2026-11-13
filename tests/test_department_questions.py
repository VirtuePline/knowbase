"""常见问题按部门/公共端分池；只写 UUID 临时 MongoDB 库与临时文件。"""
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import routes
from app.domain.schemas import AnswerResponse
from app.services import access, auth, database
from tests import test_enterprise_access as fixtures


class DepartmentQuestionsTest(unittest.TestCase):
    doc = fixtures.EnterpriseAccessTest.doc
    login_as = fixtures.EnterpriseAccessTest.login_as

    def setUp(self):
        fixtures.EnterpriseAccessTest.setUp(self)
        database.save_department("finance", "财务部门")
        self.answer = self.stack.enter_context(patch.object(
            routes.answer_service, "answer", return_value=AnswerResponse("ok", "合成答复")))

    def ask(self, question, department=None, client=None):
        payload = {"question": question}
        if department is not None:
            payload["department_id"] = department
        response = (client or self.api).post("/api/chat", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def top(self, department=None, client=None, **params):
        if department is not None:
            params["department_id"] = department
        response = (client or self.api).get("/api/questions/top", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def counts(payload):
        return {item["question"]: item["count"] for item in payload["questions"]}

    @staticmethod
    def department_ids(payload):
        return {item["id"] for item in payload["departments"]}

    def test_department_questions_are_shared_between_colleagues_but_not_other_departments(self):
        self.login_as("hr")
        self.ask("报销流程怎么走")
        self.ask("  报销流程  怎么走\n")
        first = self.top()
        self.assertEqual(first["scope"], "department")
        self.assertEqual(first["department_id"], "hr")
        self.assertEqual(self.department_ids(first), {"hr"})
        self.assertEqual(self.counts(first), {"报销流程怎么走": 2})

        self.login_as("reader")
        self.assertEqual(self.counts(self.top()), {"报销流程怎么走": 2})
        self.ask("报销流程怎么走")
        self.assertEqual(self.counts(self.top()), {"报销流程怎么走": 3})

        self.login_as("ops")
        self.assertEqual(self.top()["questions"], [])
        self.ask("设备维护流程")
        self.assertEqual(self.counts(self.top()), {"设备维护流程": 1})
        self.login_as("hr")
        self.assertEqual(self.counts(self.top()), {"报销流程怎么走": 3})

    def test_multi_department_selection_records_once_in_the_selected_pool(self):
        self.db.users.update_one({"_id": "hr"}, {"$set": {
            "department_ids": ["hr", "operations"],
            "department_roles": {"hr": "editor", "operations": "editor"}}})
        self.login_as("hr")
        self.ask("同一问题分部门统计", "hr")
        self.ask("同一问题分部门统计", "operations")
        for department in ("hr", "operations"):
            with self.subTest(department=department):
                payload = self.top(department)
                self.assertEqual(payload["department_id"], department)
                self.assertEqual(self.department_ids(payload), {"hr", "operations"})
                self.assertEqual(self.counts(payload), {"同一问题分部门统计": 1})
        self.assertEqual(self.db.question_stats.count_documents({"question": "同一问题分部门统计"}), 2)

        self.login_as("boss")
        data = self.top("hr")
        self.assertEqual(self.department_ids(data), {"hr", "operations", "finance"})
        self.assertEqual(self.counts(data), {"同一问题分部门统计": 1})
        self.assertEqual(self.top("finance")["questions"], [])

    def test_public_questions_are_shared_across_guests_and_ignore_management_cookies_or_department_parameters(self):
        self.login_as("hr")
        self.ask("部门内部申请流程")
        other_guest = TestClient(self.search.app)
        self.addCleanup(other_guest.close)
        self.ask("公共访问如何查询", client=self.search)
        self.ask("公共访问如何查询", client=other_guest)
        self.assertNotEqual(self.search.cookies.get(auth.PUBLIC_COOKIE), other_guest.cookies.get(auth.PUBLIC_COOKIE))
        other_guest.cookies.set(auth.SESSION_COOKIE, auth.create_session("boss"))
        self.ask("公共访问如何查询", department="finance", client=other_guest)

        for client in (self.search, other_guest):
            with self.subTest(client=id(client)):
                payload = self.top("hr", client=client, scope="department:hr")
                self.assertEqual(payload["scope"], "public")
                self.assertEqual(payload["department_id"], "")
                self.assertEqual(payload["departments"], [])
                self.assertEqual(self.counts(payload), {"公共访问如何查询": 3})
        self.assertEqual(self.counts(self.top("hr")), {"部门内部申请流程": 1})
        rows = list(self.db.question_stats.find({"question": "公共访问如何查询"}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scope"], "public")
        self.assertEqual(rows[0]["count"], 3)

    def test_an_unauthorized_department_is_rejected_before_answering_or_recording(self):
        self.login_as("hr")
        before = self.db.question_stats.count_documents({})
        self.answer.reset_mock()
        for department in ("finance", "operations", "does-not-exist"):
            with self.subTest(department=department):
                response = self.api.get("/api/questions/top", params={"department_id": department})
                self.assertEqual(response.status_code, 403, response.text)
                response = self.api.post("/api/chat", json={
                    "question": "不应进入别的部门", "department_id": department})
                self.assertEqual(response.status_code, 403, response.text)
        self.answer.assert_not_called()
        self.assertEqual(self.db.question_stats.count_documents({}), before)

    def test_a_user_without_a_department_can_chat_without_reading_or_writing_another_pool(self):
        self.login_as("ops")
        self.ask("默认部门已有问题")
        self.ask("公共已有问题", client=self.search)
        auth.create_user("unassigned", "Password123", department_ids=[])
        self.login_as("unassigned")
        data = self.top()
        self.assertEqual(data["scope"], "none")
        self.assertEqual(data["department_id"], "")
        self.assertEqual(data["departments"], [])
        self.assertEqual(data["questions"], [])
        before = self.db.question_stats.count_documents({})
        self.ask("没有归属的提问")
        self.assertEqual(self.db.question_stats.count_documents({}), before)
        self.assertEqual(self.api.get("/api/questions/top?department_id=operations").status_code, 403)

    def test_legacy_global_or_personal_history_is_not_reused_for_department_or_public_questions(self):
        self.db.question_stats.insert_many([
            {"question": "旧全局问题含内部信息", "count": 999, "last_at": database.now_iso()},
            {"scope": "user:hr", "question": "旧账号私人问题", "count": 999, "last_at": database.now_iso()},
            {"scope": "user:guest:old", "question": "旧访客独立问题", "count": 999, "last_at": database.now_iso()},
        ])
        self.login_as("hr")
        self.assertEqual(self.top()["questions"], [])
        self.assertEqual(self.top(client=self.search)["questions"], [])
        self.ask("新部门问题")
        self.ask("新公共问题", client=self.search)
        self.assertEqual(self.counts(self.top()), {"新部门问题": 1})
        self.assertEqual(self.counts(self.top(client=self.search)), {"新公共问题": 1})

    def test_recent_questions_keep_scope_date_limits_and_a_stable_tie_order(self):
        now = database.now_iso()
        old = (datetime.now(database.CN_TZ) - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        self.db.question_stats.insert_many([
            {"scope": "department:hr", "question": question, "count": 1, "last_at": now}
            for question in ("zzz", "aaa", "mmm")
        ] + [
            {"scope": "department:hr", "question": "过期问题", "count": 99, "last_at": old},
            {"scope": "department:operations", "question": "其他部门热门问题", "count": 999, "last_at": now},
            {"scope": "public", "question": "公共热门问题", "count": 999, "last_at": now},
        ])
        self.login_as("hr")
        data = self.top(limit=2)
        self.assertEqual([item["question"] for item in data["questions"]], ["aaa", "mmm"])
        self.assertEqual(self.top(limit=1, days=9)["questions"], [{"question": "过期问题", "count": 99}])

    def test_concurrent_updates_create_one_counter_per_department_and_normalized_question(self):
        def record(_number):
            with access.identity_scope(self.actors["hr"]):
                database.record_question("  部门并发\t统计 ")

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(record, range(16)))
        rows = list(self.db.question_stats.find({"scope": "department:hr", "question": "部门并发统计"}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 16)
        self.login_as("reader")
        self.assertEqual(self.counts(self.top()), {"部门并发统计": 16})

    def test_deleting_a_department_removes_its_questions_and_reusing_the_id_starts_empty(self):
        self.login_as("hr")
        self.ask("旧部门专属问题")
        self.ask("公共问题需要保留", client=self.search)
        self.login_as("ops")
        self.ask("其他部门问题需要保留")
        self.login_as("boss")
        deleted = self.api.request("DELETE", "/api/departments/hr", json={"target_department_id": "operations"})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.db.question_stats.count_documents({"scope": "department:hr"}), 0)
        self.assertEqual(self.counts(self.top("operations")), {"其他部门问题需要保留": 1})
        self.assertEqual(self.counts(self.top(client=self.search)), {"公共问题需要保留": 1})
        recreated = self.api.post("/api/departments", json={"id": "hr", "name": "新人事部门"})
        self.assertEqual(recreated.status_code, 200, recreated.text)
        self.assertEqual(self.top("hr")["questions"], [])

    def test_management_requires_login_and_password_setup_before_accessing_department_questions(self):
        self.assertEqual(self.api.get("/api/questions/top").status_code, 401)
        self.assertEqual(self.api.post("/api/chat", json={"question": "未登录不能提问"}).status_code, 401)
        auth.create_user("first-login", department_ids=["hr"])
        self.login_as("first-login")
        self.assertEqual(self.api.get("/api/questions/top").status_code, 403)
        self.assertEqual(self.api.post("/api/chat", json={"question": "改密前不能提问"}).status_code, 403)
        self.answer.assert_not_called()

    def test_error_answers_do_not_increment_the_selected_department_counter(self):
        self.login_as("hr")
        self.answer.return_value = AnswerResponse("error", "合成错误")
        self.ask("错误处理不计入热问")
        self.assertEqual(self.top()["questions"], [])


if __name__ == "__main__":
    unittest.main()
