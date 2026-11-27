import unittest
from datetime import date

from app.core.production.intent import ProductionQuery, parse_production_question


class ProductionIntentTest(unittest.TestCase):
    def parse(self, text):
        return parse_production_question(text, today=date(2026, 1, 5))

    def test_period_crosses_calendar_boundaries(self):
        cases = {
            "昨天的产量": (date(2026, 1, 4), date(2026, 1, 5)),
            "本周的产量": (date(2026, 1, 5), date(2026, 1, 6)),
            "上周的产量": (date(2025, 12, 29), date(2026, 1, 5)),
            "上个月的产量": (date(2025, 12, 1), date(2026, 1, 1)),
            "最近7天的产量": (date(2025, 12, 30), date(2026, 1, 6)),
            "2025年12月31日至2026年1月1日的产量": (date(2025, 12, 31), date(2026, 1, 2)),
        }
        for question, bounds in cases.items():
            with self.subTest(question=question):
                self.assertEqual(self.parse(question), ProductionQuery(("output",), *bounds))

    def test_requested_metrics_and_one_group(self):
        query = self.parse("今天各产线产量、良品数、不良品数和良率分别是多少？")
        self.assertEqual(query.metrics, ("output", "good", "defect", "yield_rate"))
        self.assertEqual(query.group_by, "line")
        self.assertEqual(self.parse("本月生产工单有多少个？").metrics, ("order_count",))

    def test_only_exact_filters_are_passed_and_quoted_identifiers_remain_literal(self):
        query = self.parse('请查询昨天一号线夜班产品="昨日良品 P01"的产量')
        self.assertEqual(query.metrics, ("output",))
        self.assertEqual(dict(query.filters), {"line": "一号线", "product": "昨日良品 P01", "shift": "夜班"})
        self.assertEqual(self.parse("昨日工单=WO-21的产量").filters, (("order_id", "WO-21"),))
        self.assertEqual(self.parse("今天 B 线的产量").filters, (("line", "B线"),))

    def test_how_to_documents_and_log_questions_use_kb(self):
        for question in ("生产报工数量不对，该怎么处理？", "MES 产量怎么查？", "良品率计算公式",
                         "今天产量异常的日志怎么看", "今年的年假额度是多少？"):
            with self.subTest(question=question):
                self.assertIsNone(self.parse(question))

    def test_ambiguous_or_invalid_dates_never_create_query(self):
        for question in ("各产线产量", "2025-02-29产量", "最近0天的产量", "最近32天的产量",
                         "今天和昨天的产量", "2025-12-30 至 2025-12-01 的产量"):
            with self.subTest(question=question):
                self.assertIsInstance(self.parse(question), str)

    def test_unknown_requested_scope_never_turns_into_unfiltered_total(self):
        for question in ("昨天车间B产量", "昨天甲部门的产量", "今天A线和B线的产量",
                         "本周按产品按班次统计产量", "昨天的计划产量", "昨天产量最高的三条线",
                         "今天产量和不合格率", "请删除昨天的生产数据", "SELECT * FROM production_daily"):
            with self.subTest(question=question):
                self.assertIsInstance(self.parse(question), str)

    def test_explicit_production_units_require_matching_report_unit(self):
        for unit in ("箱", "批", "台", "套"):
            with self.subTest(unit=unit):
                self.assertIn("按件", self.parse(f"昨天生产了多少{unit}？"))
        self.assertEqual(self.parse("昨天生产了多少件？").metrics, ("output",))
