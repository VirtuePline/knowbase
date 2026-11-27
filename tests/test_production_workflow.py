"""生产提问经过正式问答入口，在本地 SQLite 测试报表中查询。"""
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.api import routes
from app.core.answer import service
from app.services import access, auth, production_routes


QUESTION = "2026-11-26的产量是多少？"
EMPLOYEE = {"username": "employee", "role": "user", "department_ids": ["plant"]}


class ProductionWorkflowTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        database_file = Path(directory) / "reports.sqlite"
        connection = sqlite3.connect(database_file)
        try:
            connection.executescript("""
                CREATE TABLE production_daily (
                    production_date DATE, department_id TEXT, line_name TEXT, product_code TEXT,
                    shift_name TEXT, order_no TEXT, output_qty INTEGER, good_qty INTEGER, defect_qty INTEGER
                );
                INSERT INTO production_daily VALUES
                    ('2026-11-26', 'plant', 'A', 'P1', '早班', 'WO-1', 517, 510, 7),
                    ('2026-11-26', 'private', 'B', 'P2', '夜班', 'WO-2', 19331, 19300, 31),
                    ('2026-11-26', 'unconfigured', 'C', 'P3', '夜班', 'WO-4', 34567, 34000, 567),
                    ('2026-11-25', 'plant', 'A', 'P1', '早班', 'WO-3', 211, 210, 1);
            """)
        finally:
            connection.close()
        self.settings = replace(
            service.settings, production_sql_enabled=True,
            production_sql_url="sqlite:///" + database_file.as_posix(),
            production_sql_view="production_daily", production_sql_departments=("plant", "private"),
        )
        self.enterContext(patch.object(service, "settings", self.settings))
        self.enterContext(access.identity_scope(EMPLOYEE))
        self.prepare_route = self.enterContext(patch(
            "app.services.production_routes.prepare_route",
            side_effect=lambda plan, *args, **kwargs: SimpleNamespace(id="test", plan=plan),
        ))
        self.record_execution = self.enterContext(patch("app.services.production_routes.record_execution"))
        self.lookup = self.enterContext(patch.object(service, "_top_hits", return_value=[]))
        self.complete = self.enterContext(patch.object(service, "llm_complete",
                                                      side_effect=AssertionError("unexpected model call")))
        self.rewrite = self.enterContext(patch.object(service, "rewrite_query",
                                                     side_effect=AssertionError("unexpected history rewrite")))
        self.web = self.enterContext(patch.object(service, "web_search",
                                                 side_effect=AssertionError("unexpected web search")))
        self.statements = []

        def record_statement(conn, cursor, statement, parameters, context, executemany):
            self.statements.append(statement)

        event.listen(Engine, "before_cursor_execute", record_statement)
        self.addCleanup(event.remove, Engine, "before_cursor_execute", record_statement)

    def ask(self):
        return service.answer(QUESTION, fast_mode=False, llm_polish=True, web_search=True)

    def test_employee_gets_only_own_department_and_requested_day(self):
        result = self.ask()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.answer_origin, "production_sql")
        self.assertIn("517", result.answer)
        self.assertNotIn("19331", result.answer)
        self.assertFalse(result.llm_used)
        self.assertFalse(any(source.doc_id for source in result.sources))
        self.assertTrue(self.statements)
        self.lookup.assert_not_called()
        self.prepare_route.assert_called_once()
        self.record_execution.assert_called_once()

    def test_platform_admin_is_limited_to_configured_departments(self):
        with access.identity_scope({"username": "admin", "role": "admin", "department_ids": []}):
            result = self.ask()
        self.assertEqual(result.status, "ok")
        self.assertIn("19848", result.answer.replace(",", ""))
        self.lookup.assert_not_called()

    def test_unauthorized_identities_never_query_the_production_database(self):
        denied = [
            None,
            {"role": "admin", "department_ids": ["plant"]},
            {**EMPLOYEE, "public_only": True},
            {**EMPLOYEE, "must_change_password": True},
            {**EMPLOYEE, "department_ids": ["sales"]},
            {"username": "guest", "role": "guest", "department_ids": ["plant"]},
        ]
        for identity in denied:
            with self.subTest(identity=identity), access.identity_scope(identity):
                result = self.ask()
                self.assertNotEqual(result.status, "ok")
                self.assertNotIn("517", result.answer)
                self.assertEqual(self.statements, [])
        self.lookup.assert_not_called()
        self.prepare_route.assert_not_called()
        self.record_execution.assert_not_called()

    def test_disabled_route_stops_before_report_execution(self):
        self.prepare_route.side_effect = production_routes.RouteDisabled()
        result = self.ask()
        self.assertEqual(result.answer_origin, "production_sql")
        self.assertNotEqual(result.status, "ok")
        self.assertIn("停用", result.answer)
        self.assertEqual(self.statements, [])
        self.record_execution.assert_not_called()
        self.lookup.assert_not_called()

    def test_report_failure_does_not_generate_or_guess_a_value(self):
        missing_view = replace(self.settings, production_sql_view="missing_production_view")
        with patch.object(service, "settings", missing_view):
            result = self.ask()
        self.assertNotEqual(result.status, "ok")
        self.assertNotIn("517", result.answer)
        self.assertNotIn("no such table", result.answer)
        self.assertFalse(result.llm_used)
        self.assertEqual(self.record_execution.call_args.args[-1], "error")
        self.lookup.assert_not_called()

    def test_how_to_question_about_production_reporting_stays_in_knowledge_base(self):
        with patch.object(service, "allowed_document_ids", return_value=set()), \
                patch.object(service, "authorized_hits", side_effect=lambda hits: hits), \
                patch.object(service.network_policy, "external_apis_enabled", return_value=False):
            result = service.answer("生产报工数量不对，该怎么处理？", fast_mode=True)
        self.assertNotEqual(result.answer_origin, "production_sql")
        self.lookup.assert_called_once()
        self.assertEqual(self.statements, [])

    def test_chat_keeps_production_data_out_of_history_and_shared_questions(self):
        history = [{"role": "assistant", "content": "上一轮知识库回答"}]
        request = routes.ChatRequest(
            question=QUESTION, conversation_id="previous", history=history,
            fast_mode=False, llm_polish=True, web_search=True,
        )
        with patch.object(routes.database, "list_departments", return_value=[{"id": "plant"}]), \
                patch.object(routes.database, "access_revision", return_value=1), \
                patch.object(routes.conversations, "load", return_value=history), \
                patch.object(routes.conversations, "save") as save, \
                patch.object(routes.database, "record_question") as record:
            response = routes.chat(request)
        self.assertEqual(response["answer_origin"], "production_sql")
        self.assertIn("517", response["answer"])
        self.assertEqual(response["conversation_id"], "")
        save.assert_not_called()
        record.assert_not_called()
        self.rewrite.assert_not_called()
        self.complete.assert_not_called()
        self.web.assert_not_called()


class ProductionAdminApiTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.admin_router)
        self.api = self.enterContext(TestClient(app))
        self.session = self.enterContext(patch.object(auth, "session_identity"))
        self.route = {"id": "daily", "enabled": True}
        self.routes = self.enterContext(patch.object(production_routes, "list_routes", return_value=[self.route]))
        self.history = self.enterContext(patch.object(production_routes, "list_history", return_value=[]))
        self.update = self.enterContext(patch.object(production_routes, "set_enabled"))
        self.base = "/api/admin/production-sql"

    def test_guest_employee_and_department_manager_cannot_use_management_api(self):
        actors = [
            (None, 401),
            ({**EMPLOYEE, "must_change_password": False}, 403),
            ({**EMPLOYEE, "department_roles": {"plant": "admin"}, "must_change_password": False}, 403),
        ]
        for identity, expected in actors:
            self.session.return_value = identity
            with self.subTest(identity=identity):
                self.assertEqual(self.api.get(self.base + "/routes").status_code, expected)
                self.assertEqual(self.api.get(self.base + "/history").status_code, expected)
                self.assertEqual(self.api.patch(self.base + "/routes/daily", json={"enabled": False}).status_code, expected)
        self.routes.assert_not_called()
        self.history.assert_not_called()
        self.update.assert_not_called()

    def test_platform_admin_lists_pages_disables_and_reenables_route(self):
        self.session.return_value = {"username": "admin", "role": "admin", "must_change_password": False}

        def set_enabled(route_id, enabled):
            self.assertEqual(route_id, self.route["id"])
            self.route["enabled"] = enabled
            return True

        self.update.side_effect = set_enabled
        for enabled in [False, True]:
            changed = self.api.patch(self.base + "/routes/daily", json={"enabled": enabled})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json(), {"id": "daily", "enabled": enabled})
            listed = self.api.get(self.base + "/routes?limit=20&offset=40")
            self.assertEqual(listed.json()["routes"][0]["enabled"], enabled)
            self.routes.assert_called_with(limit=20, offset=40)
        self.assertEqual(self.api.get(self.base + "/history?limit=5&offset=10").json(), {"history": []})
        self.history.assert_called_once_with(limit=5, offset=10)
        self.assertEqual(self.api.patch(self.base + "/routes/daily", json={"enabled": "false"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
