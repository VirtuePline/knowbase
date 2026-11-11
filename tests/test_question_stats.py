"""提问频率统计：记录累加与最近 7 天热门查询。"""
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

from app.services import database


class FakeQuestionStats:
    """question_stats 集合的最小可用替身。"""

    def __init__(self, now_iso: str):
        self.docs: dict[str, dict] = {}
        self.now_iso = now_iso

    def update_one(self, query, update, upsert=False):
        key = query["question"]
        doc = self.docs.setdefault(key, {"question": key, "count": 0, "last_at": ""})
        doc["count"] += update["$inc"]["count"]
        doc["last_at"] = update["$set"]["last_at"]

    class _Cursor:
        def __init__(self, items):
            self.items = items

        def sort(self, key, direction):
            return self

        def limit(self, n):
            return self.items[:n]

    def find(self, query, projection=None):
        since = query["last_at"]["$gte"]
        return self._Cursor([d for d in self.docs.values() if d["last_at"] >= since])


class QuestionStatsTest(unittest.TestCase):
    def _db(self, question_stats):
        fake = type("DB", (), {"question_stats": question_stats})()
        return patch.object(database.mongo, "db", return_value=fake)

    def test_record_and_top_recent(self):
        stats = FakeQuestionStats(database.now_iso())
        with self._db(stats):
            for q in ["扫码枪扫不上", "扫码枪扫不上", "扫码枪扫不上", "音响没声音", "  网页  打不开 "]:
                database.record_question(q)
            top = database.top_questions(days=7, limit=4)
            self.assertEqual([t["question"] for t in top][:2],
                             ["扫码枪扫不上", "音响没声音"])
            self.assertEqual(top[0]["count"], 3)
            self.assertIn("网页打不开", [t["question"] for t in top])   # 空白已归一化

    def test_top_excludes_questions_older_than_days(self):
        stats = FakeQuestionStats(database.now_iso())
        old = (datetime.now(database.CN_TZ) - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        stats.docs["旧问题"] = {"question": "旧问题", "count": 99, "last_at": old}
        stats.docs["新问题"] = {"question": "新问题", "count": 1,
                               "last_at": database.now_iso()}
        with self._db(stats):
            top = database.top_questions(days=7, limit=4)
            self.assertEqual([t["question"] for t in top], ["新问题"])

    def test_empty_question_ignored(self):
        stats = FakeQuestionStats(database.now_iso())
        with self._db(stats):
            database.record_question("   ")
            self.assertEqual(stats.docs, {})


if __name__ == "__main__":
    unittest.main()
