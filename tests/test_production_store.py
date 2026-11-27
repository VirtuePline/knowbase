"""生产报表 SELECT 的日期、权限、聚合口径和可复用绑定参数。"""
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app.core.production.intent import ProductionQuery
from app.core.production import store


class ProductionStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.file = self.directory / "生产 #1.sqlite"
        self.url = "sqlite:///" + self.file.as_posix()
        self.view = "main.production_daily"
        self.plan = ProductionQuery(("output", "good", "defect", "order_count", "yield_rate"),
                                    date(2026, 11, 1), date(2026, 11, 3))
        with closing(sqlite3.connect(self.file)) as db:
            db.executescript("""
                CREATE TABLE records (
                    production_date DATE, department_id TEXT COLLATE NOCASE, line_name TEXT, product_code TEXT,
                    shift_name TEXT, order_no TEXT, output_qty INTEGER, good_qty INTEGER, defect_qty INTEGER
                );
                CREATE VIEW production_daily AS SELECT * FROM records;
                INSERT INTO records VALUES
                    ('2026-10-31','factory-a','outside','P1','day','7',1,1,0),
                    ('2026-11-01','factory-a','A','P1','day','101',10,5,5),
                    ('2026-11-02','factory-a','A','P1','day','101',90,90,0),
                    ('2026-11-02','factory-b','B','P2','night','102',1000,1000,0),
                    ('2026-11-02','FACTORY-A','B','P2','night','103',2000,2000,0),
                    ('2026-11-03','factory-a','outside','P1','day','8',1,1,0);
            """)

    def query(self, plan=None, departments=None):
        return store.query_production(plan or self.plan, department_ids={"factory-a"} if departments is None else departments,
                                      url=self.url, view=self.view)

    def add_rows(self, rows):
        with closing(sqlite3.connect(self.file)) as db, db:
            db.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def test_weighted_yield_distinct_orders_and_date_department_scope(self):
        result = self.query()
        self.assertEqual(result.rows, ({"output": 100, "good": 95, "defect": 5,
                                        "order_count": 1, "yield_rate": Decimal("95.00")},))
        self.assertFalse(result.truncated)
        grouped = self.query(replace(self.plan, metrics=("output",), group_by="day"))
        self.assertEqual(grouped.rows, ({"day": "2026-11-01", "output": 10}, {"day": "2026-11-02", "output": 90}))

    def test_empty_zero_and_partially_missing_are_distinct(self):
        self.add_rows([("2026-11-01", "factory-a", "zero", "X", "day", "Z", 0, 0, 0),
                       ("2026-11-01", "factory-a", "null", "X", "day", " ", 20, None, 1),
                       ("2026-11-02", "factory-a", "null", "X", "day", "Y", 10, 10, 0)])
        def rows(line):
            return self.query(replace(self.plan, filters=(("line", line),))).rows
        self.assertEqual(rows("unmatched"), ())
        self.assertEqual(rows("zero"), ({"output": 0, "good": 0, "defect": 0, "order_count": 1, "yield_rate": None},))
        self.assertEqual(rows("null"), ({"output": 30, "good": None, "defect": 1, "order_count": None, "yield_rate": None},))

    def test_same_template_changes_all_parameters_without_reusing_results(self):
        plan = replace(self.plan, metrics=("output",), start=date(2026, 11, 1), end=date(2026, 11, 2),
                       filters=(("line", "A"), ("product", "P1")))
        next_plan = replace(plan, start=date(2026, 11, 2), end=date(2026, 11, 3),
                            filters=(("product", "P2"), ("line", "B")))
        hits_before = store._statement.cache_info().hits
        self.assertEqual(self.query(plan).rows, ({"output": 10},))
        self.assertEqual(self.query(next_plan, {"factory-b"}).rows, ({"output": 1000},))
        self.assertGreater(store._statement.cache_info().hits, hits_before)
        self.assertEqual(self.query(next_plan, {"factory-a"}).rows, ())
        for driver in ("mysql+pymysql", "sqlite", "mssql", "postgresql"):
            with self.subTest(driver=driver):
                url = driver + "://"
                sql = store.parameterized_sql(plan, url=url, view=self.view)
                self.assertEqual(sql, store.parameterized_sql(next_plan, url=url, view=self.view))
                for name in ("start_date", "end_date", "department_ids", "filter_product", "filter_line"):
                    self.assertIn(":" + name, sql)
                for private_value in ("P1", "P2", "factory-a", "2026-11", "__[POSTCOMPILE_"):
                    self.assertNotIn(private_value, sql)

    def test_group_limit_and_bound_literal_label(self):
        line = "线'A"
        records = [("2026-11-01", "factory-a", line, f"P{n:03d}", "day", "W", 1, 1, 0) for n in range(103)]
        self.add_rows(records)
        plan = replace(self.plan, metrics=("output",), group_by="product", filters=(("line", line),))
        result = self.query(plan)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.rows), 100)
        self.assertEqual(result.rows[0], {"product": "P000", "output": 1})
        sql = store.parameterized_sql(plan, view=self.view, url="mysql+pymysql://")
        self.assertIn("LIMIT 101", sql)
        self.assertNotIn(line, sql)

    def test_invalid_scope_dates_identifiers_and_fields_are_rejected(self):
        for bad_scope in (set(), "factory-a"):
            with self.subTest(scope=bad_scope), self.assertRaises(store.ProductionStoreError):
                self.query(departments=bad_scope)
        for args in ({"end": self.plan.start + timedelta(days=32)}, {"end": self.plan.start},
                     {"group_by": "department_id"}, {"filters": (("department_id", "factory-b"),)},
                     {"filters": (("product", "P1"), ("product", "P2"))}, {"metrics": ("unconfigured",)}):
            with self.subTest(args=args), self.assertRaises(store.ProductionStoreError):
                self.query(replace(self.plan, **args))
        self.view = "report table"
        with self.assertRaises(store.ProductionStoreError):
            self.query()

    def test_sqlite_file_must_exist_and_is_opened_read_only(self):
        with store._engine(self.url).connect() as connection, self.assertRaises(OperationalError):
            connection.exec_driver_sql("INSERT INTO records (output_qty) VALUES (10)")
        missing = self.directory / "absent.sqlite"
        self.url = "sqlite:///" + missing.as_posix()
        with self.assertRaises(store.ProductionStoreError):
            self.query()
        self.assertFalse(missing.exists())

    def test_mysql_driver_uses_bounded_timeouts(self):
        with patch.object(store, "create_engine") as create:
            store._engine("mysql+pymysql://example.invalid/report_test")
        self.assertEqual(create.call_args.kwargs["connect_args"],
                         {"connect_timeout": 5, "read_timeout": 15, "write_timeout": 5})


if __name__ == "__main__":
    unittest.main()
