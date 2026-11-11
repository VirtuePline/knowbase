"""运维周报：设置校验、本周记录收集与定时生成幂等。"""
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
        doc_type=doc_type, uploaded_by="acc", uploaded_by_name=owner,
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
    """本周记录收集：按运维人与日期过滤，事项压缩到 20 字。"""

    def test_collect_records_filters_owner_and_week(self):
        week = "2026-11-09"   # 周一
        records = [
            make_record("a1", "贴标机闪退，重启工控机后恢复正常运行", "张三", "2026-11-09 10:00:00"),
            make_record("a2", "本周内但属于别人的记录，不应出现", "李四", "2026-11-10 09:00:00"),
            make_record("a3", "上周的记录不应出现", "张三", "2026-11-02 09:00:00"),
            make_record("a4", "基础文档不算运维记录", "张三", "2026-11-11 09:00:00", doc_type="doc"),
        ]
        with patch.object(weekly.database, "list_all", return_value=records):
            items = weekly.collect_records(week, "张三")
            self.assertEqual([i["doc_id"] for i in items], ["a1"])
            self.assertTrue(all(len(i["text"]) <= 20 for i in items))

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
                             side_effect=lambda w, p, items, at: saved.append((w, p, items))), \
                patch.object(weekly.database, "now_iso", return_value="2026-11-13 18:05:00"):
            self.assertTrue(weekly.run_scheduled_once(now))
            self.assertEqual([(w, p) for w, p, _ in saved], [("2026-11-09", "张三")])
            # 事项压缩到 20 字以内
            expected = "箱签打印不出，检查打印机地址后恢复正常使用"[:20]
            self.assertEqual(saved[0][2][0]["text"], expected)
            self.assertLessEqual(len(saved[0][2][0]["text"]), 20)

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
