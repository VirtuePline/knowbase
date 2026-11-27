"""生产问答路由的持久化与重绑定。集成用例只写随机命名的测试库。"""
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pymongo import MongoClient
from pymongo.errors import AutoReconnect, DuplicateKeyError

from app.config import settings
from app.core.production.intent import ProductionQuery
from app.core.answer import service
from app.services import access, production_routes as routes


SQL_SETTINGS = replace(settings, production_sql_url="mysql+pymysql://report:fake-secret@example.invalid/plant",
                       production_sql_view="plant.daily")
PLAN = ProductionQuery(("output", "good"), date(2026, 11, 26), date(2026, 11, 27),
                       "day", (("shift", "早班"), ("line", "A01")))


class ProductionRouteFailureTest(unittest.TestCase):
    def test_persistence_operations_fail_closed(self):
        with patch.object(routes.mongo, "db", side_effect=AutoReconnect("private connection details")):
            for action in [lambda: routes.prepare_route(PLAN, "昨天产量", SQL_SETTINGS),
                           lambda: routes.record_execution("route", "昨天产量", PLAN, "tester", "ok"),
                           routes.list_routes, routes.list_history, lambda: routes.set_enabled("route", False)]:
                with self.subTest(action=action), self.assertRaises(routes.RouteUnavailable) as caught:
                    action()
                self.assertNotIn("private", str(caught.exception))

    def test_concurrent_insert_cannot_reenable_route(self):
        with patch.object(routes.mongo, "db") as get_db:
            collection = get_db.return_value.production_sql_routes
            collection.find_one.return_value = None

            def competing_insert(identity, update, **options):
                collection.find_one.return_value = {**identity, "shape": update["$setOnInsert"]["shape"],
                                                    "enabled": False}
                raise DuplicateKeyError("another worker created the same route")

            collection.find_one_and_update.side_effect = competing_insert
            with self.assertRaises(routes.RouteDisabled):
                routes.prepare_route(PLAN, "昨天按天看产量和良品数", SQL_SETTINGS)
            self.assertEqual(collection.find_one.call_count, 2)


class ProductionRouteMongoTest(unittest.TestCase):
    def setUp(self):
        client = MongoClient(os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017"),
                             serverSelectionTimeoutMS=5000)
        self.addCleanup(client.close)
        self.db = client["kbtest_production_routes_" + uuid4().hex]
        self.addCleanup(client.drop_database, self.db.name)
        self.enterContext(patch.object(routes.mongo, "db", return_value=self.db))

    def test_chat_reuses_route_and_queries_fresh_values_with_current_scope(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        report = Path(folder) / "production.sqlite"
        connection = sqlite3.connect(report)
        try:
            connection.executescript("""
                CREATE TABLE production_daily(production_date DATE, department_id TEXT, line_name TEXT, output_qty INTEGER);
                INSERT INTO production_daily VALUES ('2026-11-01','one','A线',17),
                    ('2026-11-02','two','B线',63), ('2026-11-02','one','B线',751);
            """)
        finally:
            connection.close()
        cfg = replace(settings, production_sql_enabled=True, production_sql_url="sqlite:///" + report.as_posix(),
                      production_sql_view="production_daily", production_sql_departments=("one", "two"))
        self.enterContext(patch.object(service, "settings", cfg))
        first = "2026-11-01 A 线的产量"
        second = "2026-11-02 B 线的产量"
        with access.identity_scope({"username": "worker1", "department_ids": ["one"]}):
            self.assertIn("17 件", service.answer(first).answer)
        with access.identity_scope({"username": "worker2", "department_ids": ["two"]}):
            self.assertIn("63 件", service.answer(second).answer)
            result = service.answer("2026-11-03 B 线的产量")
            self.assertIn("没有生产记录", result.answer)
            template, = routes.list_routes()
            self.assertEqual(template["use_count"], 3)
            self.assertEqual(routes.list_history()[0]["outcome"], "empty")
            self.assertTrue(routes.set_enabled(template["id"], False))
            self.assertIn("停用", service.answer(second).answer)
            self.assertEqual(len(routes.list_history()), 3)

    def test_reuse_rebinds_new_values_and_does_not_read_sql_as_code(self):
        a = routes.prepare_route(PLAN, "昨天按天看产量和良品数", SQL_SETTINGS)
        new = replace(PLAN, metrics=("good", "output"), start=date(2026, 11, 1), end=date(2026, 11, 4),
                      filters=(("line", "B02"), ("shift", "夜班")))
        row = self.db.production_sql_routes.find_one({"_id": a.id})
        self.assertNotIn("fake-secret", str(row))
        for value in ("2026-11-26", "早班", "A01"):
            self.assertNotIn(value, row["sql"])
        self.assertEqual(["start_date", "end_date", "department_ids", "filter_line", "filter_shift"],
                         row["parameters"])
        self.db.production_sql_routes.update_one({"_id": a.id}, {"$set": {"sql": "仅用于演示的展示文本"}})
        with patch.object(routes.store, "parameterized_sql", side_effect=AssertionError("unexpected recompile")):
            b = routes.prepare_route(new, "另一个区间同一查询", SQL_SETTINGS)
        self.assertEqual(a.id, b.id)
        self.assertEqual(b.plan, new)
        self.assertEqual(self.db.production_sql_routes.count_documents({}), 1)
        for changed, config in [(replace(new, metrics=("defect",)), SQL_SETTINGS),
                                (replace(new, group_by="line"), SQL_SETTINGS),
                                (replace(new, filters=()), SQL_SETTINGS),
                                (new, replace(SQL_SETTINGS, production_sql_url="sqlite:///temp.sqlite")),
                                (new, replace(SQL_SETTINGS, production_sql_view="plant.other"))]:
            with self.subTest(plan=changed, view=config.production_sql_view):
                self.assertNotEqual(a.id, routes.prepare_route(changed, "另一个模板", config).id)

    def test_disabled_or_inconsistent_template_fails_closed(self):
        route = routes.prepare_route(PLAN, "昨天按天看产量和良品数", SQL_SETTINGS)
        self.assertFalse(routes.set_enabled("unknown", False))
        self.assertTrue(routes.set_enabled(route.id, False))
        self.assertTrue(routes.set_enabled(route.id, False))
        with self.assertRaises(routes.RouteDisabled):
            routes.prepare_route(PLAN, "同类问题", SQL_SETTINGS)
        self.assertTrue(routes.set_enabled(route.id, True))
        self.assertEqual(routes.prepare_route(PLAN, "同类问题", SQL_SETTINGS).id, route.id)
        self.db.production_sql_routes.update_one({"_id": route.id}, {"$unset": {"enabled": ""}})
        with self.assertRaises(routes.RouteUnavailable):
            routes.prepare_route(PLAN, "同类问题", SQL_SETTINGS)
        self.db.production_sql_routes.update_one({"_id": route.id},
                                                {"$set": {"enabled": True, "shape.filter_fields": []}})
        with self.assertRaises(routes.RouteUnavailable):
            routes.prepare_route(PLAN, "同类问题", SQL_SETTINGS)

    def test_concurrent_calls_record_separate_history_with_consistent_statistics(self):
        def use(i):
            value = f"A{i}"
            plan = replace(PLAN, filters=(("line", value),))
            question = f"昨天产线={value}的产量和良品数"
            route = routes.prepare_route(plan, question, SQL_SETTINGS)
            status = ("ok", "empty", "error")[i % 3]
            routes.record_execution(route.id, question, route.plan, f"user{i}", status)
            return route.id

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = set(pool.map(use, range(12)))
        self.assertEqual(len(ids), 1)
        route_id, = ids
        template, = routes.list_routes()
        self.assertEqual(template["id"], route_id)
        self.assertEqual(template["use_count"], 12)
        self.assertTrue(template["enabled"])
        self.assertEqual(routes.list_routes(limit=1, offset=1), [])
        all_rows = routes.list_history()
        self.assertEqual(routes.list_history(limit=3, offset=2), all_rows[2:5])
        self.assertEqual({r["outcome"] for r in all_rows}, {"ok", "empty", "error"})
        self.assertEqual(template["last_used_at"], max(r["executed_at"] for r in all_rows))
        self.assertEqual({r["parameters"]["filter_line"] for r in all_rows}, {f"A{i}" for i in range(12)})
        for row in all_rows:
            index = int(row["username"].removeprefix("user"))
            self.assertEqual(row["question"], f"昨天产线=A{index}的产量和良品数")
            self.assertEqual(row["route_id"], route_id)
            self.assertEqual(row["parameters"], {"start_date": "2026-11-26", "end_date": "2026-11-27",
                                                "filter_line": f"A{index}"})
            self.assertEqual(set(row), {"question", "route_id", "username", "executed_at", "outcome", "parameters"})
        with self.assertRaises(ValueError):
            routes.record_execution(route_id, "实际问题", PLAN, "user", "包含数据或错误详情的文字")
        self.assertEqual(routes.list_routes()[0]["use_count"], 12)
