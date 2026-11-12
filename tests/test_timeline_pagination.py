"""时间线按日期和条件在 MongoDB 分页；只使用独立临时库，不读写业务文档。"""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.api import routes
from app.domain.schemas import DocRecord
from app.main import search_app
from app.services import auth, database, mongo, timeline


class TimelinePaginationTest(unittest.TestCase):
    def setUp(self):
        self.client = MongoClient(
            os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017"),
            serverSelectionTimeoutMS=5000,
        )
        self.addCleanup(self.client.close)
        self.client.admin.command("ping")
        self.db_name = f"kbtest_timeline_{uuid4().hex}"
        self.addCleanup(self.client.drop_database, self.db_name)
        self.db = self.client[self.db_name]
        db_patch = patch.object(mongo, "db", return_value=self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        # 分页不能退回读取全部文档或全部人工状态。
        for name in ("list_all", "get_event_status_map"):
            read_patch = patch.object(database, name, side_effect=AssertionError(f"full read: {name}"))
            read_patch.start()
            self.addCleanup(read_patch.stop)

    def add(self, doc_id, *, name=None, summary="2026年11月12日，现象：网络断开。",
            uploaded_at="2026-11-12 09:00:00", category="网络问题", categories=None,
            vendor="", resolved="unresolved", status="ready", parent_id="", root_id="",
            doc_type="record"):
        database.add(DocRecord(
            id=doc_id, name=name or doc_id, stored_name=f"{doc_id}.md", ext=".md",
            size=100, sha256=doc_id, chunks=1, uploaded_at=uploaded_at, status=status,
            summary=summary, category=category, categories=categories or [], vendor=vendor,
            resolved=resolved, parent_id=parent_id, root_id=root_id, doc_type=doc_type,
            uploaded_by="engineer", uploaded_by_name="张工",
        ))

    def test_default_thirty_and_following_pages_have_stable_order_and_global_counts(self):
        for number in range(65):
            self.add(f"d{number:03}", name="同日同名问题")
        pages = [timeline.build(offset=offset) for offset in (0, 30, 60)]
        self.assertEqual([len(page["events"]) for page in pages], [30, 30, 5])
        self.assertEqual([page["has_more"] for page in pages], [True, True, False])
        self.assertEqual([page["next_offset"] for page in pages], [30, 60, 65])
        ids = [event["doc_id"] for page in pages for event in page["events"]]
        self.assertEqual(ids, [f"d{number:03}" for number in reversed(range(65))])
        for page in pages:
            self.assertEqual(page["total"], 65)
            self.assertEqual(page["issue_count"], 65)
            self.assertEqual(page["categories"], [{"name": "网络问题", "count": 65}])
            self.assertEqual(page["status_counts"]["no"], 65)
        beyond = timeline.build(offset=80)
        self.assertEqual(beyond["events"], [])
        self.assertEqual(beyond["total"], 65)
        self.assertFalse(beyond["has_more"])

    def test_date_range_uses_summary_date_and_includes_both_boundary_days(self):
        self.add("start", summary="2026/11/1，现象：网口不通。", uploaded_at="2026-12-20 18:00:00")
        self.add("end", summary="2026 年 11 月 30 日，现象：网络断开。", uploaded_at="2026-12-21 18:00:00")
        self.add("fallback", summary="现象：交换机端口不通。", uploaded_at="2026-11-15 23:59:59")
        self.add("before", summary="2026年10月31日，现象：网络断开。")
        self.add("after", summary="2026年12月1日，现象：网络断开。")
        result = timeline.build(start_date="2026-11-01", end_date="2026-11-30")
        self.assertEqual([(event["doc_id"], event["date"]) for event in result["events"]],
                         [("end", "2026-11-30"), ("fallback", "2026-11-15"), ("start", "2026-11-01")])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["categories"], [{"name": "网络问题", "count": 3}])
        after = timeline.build(start_date="2026-11-30")
        self.assertEqual([event["doc_id"] for event in after["events"]], ["after", "end"])
        before = timeline.build(end_date="2026-11-01")
        self.assertEqual([event["doc_id"] for event in before["events"]], ["start", "before"])

    def test_month_precision_matches_overlapping_range_without_inventing_a_day(self):
        self.add("month", summary="2026年11月，现象：网络断开。", uploaded_at="2026-12-01 10:00:00")
        self.add("previous-month", summary="2026年10月，现象：网络断开。")
        self.add("full-date", summary="月份：2026年1月；2026年11月16日，现象：网络断开。")
        result = timeline.build(start_date="2026-11-15", end_date="2026-11-16")
        self.assertEqual([(event["doc_id"], event["date"]) for event in result["events"]],
                         [("full-date", "2026-11-16"), ("month", "2026-11")])

    def test_filters_run_before_pagination_and_counts_use_the_full_matching_scope(self):
        for number in range(40):
            self.add(f"new-{number:02}", summary="2026年11月20日，现象：网络断开。")
        for doc_id, resolved, vendor in (("a", "resolved", "华智"), ("b", "unresolved", "华智"),
                                         ("c", "improving", "华智"), ("d", "temporary", "华智"),
                                         ("e", "resolved", "另一厂商")):
            self.add(doc_id, summary="2026年11月1日，现象：远程系统报警。", category="厂商系统",
                     categories=["厂商系统", "网络问题"], vendor=vendor, resolved=resolved)
        self.add("ledger", category="常识库", categories=["常识库", "网络问题"],
                 doc_type="doc", resolved="resolved")
        self.add("failed", status="failed")
        self.add("empty", summary="  \n\t")
        database.set_event_status("a", "a", "unresolved")
        database.set_event_status("b", "b", "resolved")

        result = timeline.build(category="厂商系统", vendor="华智", status="yes", limit=1)
        self.assertEqual([event["doc_id"] for event in result["events"]], ["b"])
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["has_more"])
        self.assertEqual(result["issue_count"], 45)
        self.assertEqual({item["name"]: item["count"] for item in result["categories"]},
                         {"厂商系统": 5, "网络问题": 46, "常识库": 1})
        self.assertEqual({item["name"]: item["count"] for item in result["vendors"]},
                         {"华智": 4, "另一厂商": 1})
        self.assertEqual(result["status_counts"], {"": 4, "yes": 1, "no": 1, "improving": 1, "temporary": 1})
        for status, expected_id in (("no", "a"), ("improving", "c"), ("temporary", "d")):
            with self.subTest(status=status):
                result = timeline.build(category="厂商系统", vendor="华智", status=status)
                self.assertEqual([event["doc_id"] for event in result["events"]], [expected_id])
        general = timeline.build()
        self.assertEqual(general["total"], 45)
        self.assertEqual(len(general["events"]), 30)
        self.assertNotIn("ledger", [event["doc_id"] for event in general["events"]])
        knowledge = timeline.build(category="常识库")
        self.assertEqual([event["doc_id"] for event in knowledge["events"]], ["ledger"])

    def test_page_retains_parent_and_child_metadata_outside_the_date_range(self):
        self.add("parent", name="父问题", summary="2026年1月1日，现象：报工失败。")
        self.add("child", name="后续处理", summary="2026年11月10日，现象：报工失败。",
                 parent_id="parent", root_id="parent", resolved="improving")
        self.add("failed-child", status="failed", parent_id="child", root_id="parent")
        result = timeline.build(start_date="2026-11-01", end_date="2026-11-30", limit=1)
        self.assertEqual(result["total"], 1)
        event = result["events"][0]
        self.assertEqual(event["doc_id"], "child")
        self.assertEqual(event["parent_id"], "parent")
        self.assertEqual(event["root_id"], "parent")
        self.assertEqual(event["parent_name"], "父问题")
        self.assertEqual(event["child_count"], 1)
        self.assertEqual(event["uploaded_by_name"], "张工")
        self.assertEqual(event["status"], "improving")
        self.assertEqual(database.get("parent").resolved, "unresolved")

    def test_empty_date_range_returns_empty_counts_and_no_more_pages(self):
        self.add("outside")
        result = timeline.build(start_date="2030-01-01", end_date="2030-01-31")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["issue_count"], 0)
        self.assertEqual(result["categories"], [])
        self.assertEqual(result["vendors"], [])
        self.assertTrue(all(value == 0 for value in result["status_counts"].values()))
        self.assertFalse(result["has_more"])


class TimelineApiTest(unittest.TestCase):
    def setUp(self):
        identity = {"username": "timeline-test", "display_name": "时间线测试员", "role": "admin",
                    "department_ids": ["operations"], "department_roles": {"operations": "admin"},
                    "must_change_password": False, "password_version": 0}
        session = patch.object(auth, "session_identity", side_effect=lambda token: identity if token == "timeline-session" else None)
        session.start()
        self.addCleanup(session.stop)
        self.api = TestClient(search_app)
        self.addCleanup(self.api.close)
        self.api.cookies.set(auth.SESSION_COOKIE, "timeline-session")

    def test_anonymous_timeline_requires_login_before_query(self):
        anonymous = TestClient(search_app)
        self.addCleanup(anonymous.close)
        with patch.object(routes.timeline_service, "build") as build:
            response = anonymous.get("/api/timeline")
        self.assertEqual(response.status_code, 401)
        build.assert_not_called()

    def test_default_query_forwards_only_thirty_events(self):
        payload = {"events": [], "has_more": False, "next_offset": 0, "total": 0}
        with patch.object(routes.timeline_service, "build", return_value=payload) as build:
            response = self.api.get("/api/timeline")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(build.call_args.kwargs["limit"], 30)
        self.assertEqual(build.call_args.kwargs["offset"], 0)

    def test_date_and_filter_parameters_are_forwarded_to_server_pagination(self):
        with patch.object(routes.timeline_service, "build", return_value={"events": []}) as build:
            response = self.api.get("/api/timeline", params={
                "start_date": "2026-11-01", "end_date": "2026-11-30", "category": "厂商系统",
                "vendor": "华智", "status": "improving", "offset": 30, "limit": 30,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(build.call_args.kwargs, {
            "start_date": "2026-11-01", "end_date": "2026-11-30", "category": "厂商系统",
            "vendor": "华智", "status": "improving", "offset": 30, "limit": 30,
        })

    def test_invalid_dates_ranges_page_sizes_and_status_are_rejected_before_query(self):
        cases = [
            {"start_date": "2026-02-30"}, {"end_date": "not-a-date"},
            {"start_date": "2026-12-01", "end_date": "2026-11-30"},
            {"offset": -1}, {"limit": 0}, {"limit": 101}, {"status": "resolved"},
        ]
        with patch.object(routes.timeline_service, "build") as build:
            for params in cases:
                with self.subTest(params=params):
                    response = self.api.get("/api/timeline", params=params)
                    self.assertEqual(response.status_code, 422)
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
