"""工作周报：设置校验、本周记录收集与定时生成幂等。"""
import datetime
import unittest
from unittest.mock import patch

from app.domain.schemas import DocRecord
from app.services import weekly


def make_record(doc_id: str, name: str, owner: str, uploaded_at: str,
                doc_type: str = "record") -> DocRecord:
    return DocRecord(
        id=doc_id, name=name, stored_name=f"{doc_id}.md", ext=".md", size=10,
        sha256=doc_id, chunks=1, uploaded_at=uploaded_at, status="ready",
        doc_type=doc_type, uploaded_by=owner, uploaded_by_name=owner,
    )


class WeeklySettingsTest(unittest.TestCase):
    """周报设置：缺省值、非法输入与持久化。"""

    def test_defaults_when_unset(self):
        with patch.object(weekly.database, "get_runtime_setting", return_value=""):
            conf = weekly.get_settings()
            self.assertFalse(conf["enabled"])
            self.assertEqual(conf["weekday"], 5)
            self.assertEqual(conf["time"], "18:00")
            self.assertEqual(conf["operators"], [])

    def test_save_settings_normalizes_and_validates(self):
        with patch.object(weekly.database, "set_runtime_setting") as setter:
            conf = weekly.save_settings(True, 5, "9:5", [" 张三 ", "张三", ""])
            self.assertEqual(conf["time"], "09:05")
            self.assertEqual(conf["operators"], ["张三"])
            self.assertTrue(conf["enabled"])
            setter.assert_called_once()
            with self.assertRaises(ValueError):
                weekly.save_settings(True, 8, "18:00", [])
            with self.assertRaises(ValueError):
                weekly.save_settings(True, 5, "25:00", [])


class WeeklyCollectTest(unittest.TestCase):
    """本周记录收集：按账号与日期过滤，标题完整保留并带解决状态。"""

    def test_collect_records_filters_owner_and_week(self):
        week = "2026-11-09"   # 周一
        records = [
            make_record("a1", "贴标机闪退，重启工控机后恢复正常运行", "张三", "2026-11-09 10:00:00"),
            make_record("a2", "本周内但属于别人的记录，不应出现", "李四", "2026-11-10 09:00:00"),
            make_record("a3", "上周的记录不应出现", "张三", "2026-11-02 09:00:00"),
            make_record("a4", "基础文档不算运维记录", "张三", "2026-11-11 09:00:00", doc_type="doc"),
        ]
        records[0].resolved = "resolved"
        with patch.object(weekly.database, "list_all", return_value=records):
            items = weekly.collect_records(week, "张三")
            self.assertEqual([i["doc_id"] for i in items], ["a1"])
            self.assertEqual(items[0]["doc_name"], "贴标机闪退，重启工控机后恢复正常运行")
            self.assertEqual(items[0]["resolved"], "已完成")
            self.assertEqual(items[0]["date"], "2026-11-09")

    def test_week_start_is_monday(self):
        self.assertEqual(weekly.week_start(datetime.date(2026, 11, 11)), "2026-11-09")


class WeeklyScheduleTest(unittest.TestCase):
    """定时生成：到点触发、未到点跳过。"""

    def setUp(self):
        self.conf = {"enabled": True, "weekday": 5, "time": "18:00",
                     "operators": ["张三"]}

    def test_runs_at_planned_time(self):
        # 2026-11-13 是周五，18:05 已过 18:00 计划点
        now = datetime.datetime(2026, 11, 13, 18, 5)
        saved = []
        with patch.object(weekly, "get_settings", return_value=self.conf), \
                patch.object(weekly.database, "list_all", return_value=[
                    make_record("a1", "箱签打印不出，检查打印机地址后恢复正常使用", "张三", "2026-11-11 10:00:00")]), \
                patch.object(weekly.database, "save_weekly_report",
                             side_effect=lambda w, p, items, at, markdown="": saved.append((w, p, items, markdown))), \
                patch.object(weekly.database, "now_iso", return_value="2026-11-13 18:05:00"):
            self.assertTrue(weekly.run_scheduled_once(now))
            self.assertEqual([(w, p) for w, p, _, _ in saved], [("2026-11-09", "张三")])
            # markdown：文档引用可点击，文档名后跟日期，每条标明解决状态
            md = saved[0][3]
            self.assertIn("[箱签打印不出，检查打印机地址后恢复正常使用](doc://a1)", md)
            self.assertIn("（2026-11-11）", md)
            self.assertNotIn("- [", md.split("（")[0] if "（" in md else md)   # 开头不是日期

    def test_render_markdown_lists_reference_with_status(self):
        items = [{"doc_id": "a1", "doc_name": "扫码枪扫不上", "date": "2026-11-11", "resolved": "未解决"}]
        md = weekly.render_markdown("2026-11-09", "张三", items)
        self.assertIn("[扫码枪扫不上](doc://a1)（2026-11-11）未解决", md)
        self.assertIn("## 本周工作记录", md)
        self.assertTrue(md.startswith("# 张三 的工作周报"))
        empty = weekly.render_markdown("2026-11-09", "李四", [])
        self.assertIn("本周没有工作记录", empty)

    def test_render_markdown_prefers_llm_summary(self):
        items = [{"doc_id": "a1", "doc_name": "扫码枪扫不上", "date": "2026-11-11", "resolved": "已解决"}]
        md = weekly.render_markdown("2026-11-09", "张三", items, summary="本周完成了扫码枪排查。")
        self.assertIn("本周完成了扫码枪排查。", md)
        self.assertNotIn("小结生成失败", md)
        fallback = weekly.render_markdown("2026-11-09", "张三", items, summary="")
        self.assertIn("小结生成失败", fallback)

    def test_summarize_without_entries_skips_llm(self):
        with patch.object(weekly, "_record_content", return_value=""), \
                patch.object(weekly.llm_client, "chat") as chat_mock:
            self.assertEqual(weekly.summarize("张三", "2026-11-09", [{"doc_id": "a1"}]), "")
            chat_mock.assert_not_called()

    def test_summarize_reads_records_and_returns_reply(self):
        items = [{"doc_id": "a1", "doc_name": "扫码枪扫不上", "date": "2026-11-11", "resolved": "已解决"}]
        with patch.object(weekly, "_record_content", return_value="检查扫码枪线缆。"), \
                patch.object(weekly.llm_client, "chat", return_value="本周修复了扫码枪。") as chat_mock:
            self.assertEqual(weekly.summarize("张三", "2026-11-09", items), "本周修复了扫码枪。")
            user_arg = chat_mock.call_args.args[1]
            self.assertIn("扫码枪扫不上", user_arg)
            self.assertIn("检查扫码枪线缆。", user_arg)

    def test_summarize_supports_non_technical_work(self):
        items = [{"doc_id": "a1", "doc_name": "报销流程更新", "date": "2026-11-11", "resolved": "已解决"}]
        with patch.object(weekly, "_record_content", return_value="更新差旅报销审批流程，已通知各部门。"), \
                patch.object(weekly.llm_client, "chat", return_value="本周完成报销流程更新。") as chat_mock:
            summary = weekly.summarize("finance-user", "2026-11-09", items)
        self.assertEqual(summary, "本周完成报销流程更新。")
        system_arg, user_arg = chat_mock.call_args.args
        self.assertIn("企业各部门", system_arg)
        self.assertNotIn("工厂", system_arg)
        self.assertNotIn("运维", system_arg + user_arg)
        self.assertIn("负责人：finance-user", user_arg)
        self.assertIn("更新差旅报销审批流程，已通知各部门。", user_arg)

    def test_summarize_llm_failure_returns_empty(self):
        items = [{"doc_id": "a1", "doc_name": "x", "date": "2026-11-11", "resolved": ""}]
        with patch.object(weekly, "_record_content", return_value="正文"), \
                patch.object(weekly.llm_client, "chat", side_effect=RuntimeError("boom")):
            self.assertEqual(weekly.summarize("张三", "2026-11-09", items), "")

    def test_skips_before_planned_time_and_weekday(self):
        # 周五 17:00：未到计划时间点
        with patch.object(weekly, "get_settings", return_value=self.conf):
            self.assertFalse(weekly.run_scheduled_once(datetime.datetime(2026, 11, 13, 17, 0)))
        # 周三：还没到计划星期五
        with patch.object(weekly, "get_settings", return_value=self.conf):
            self.assertFalse(weekly.run_scheduled_once(datetime.datetime(2026, 11, 11, 19, 0)))

    def test_disabled_or_empty_operators_skip(self):
        with patch.object(weekly, "get_settings",
                          return_value={**self.conf, "enabled": False}):
            self.assertFalse(weekly.run_scheduled_once(datetime.datetime(2026, 11, 13, 18, 5)))
        with patch.object(weekly, "get_settings",
                          return_value={**self.conf, "operators": []}):
            self.assertFalse(weekly.run_scheduled_once(datetime.datetime(2026, 11, 13, 18, 5)))


if __name__ == "__main__":
    unittest.main()
