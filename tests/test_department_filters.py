"""部门筛选与权限交集回归；仅写随机命名的临时 MongoDB 库和临时文件。"""
import unittest

from app.services import auth, database, kb
from tests import test_enterprise_access as fixtures


class DepartmentFilterTest(unittest.TestCase):
    doc = fixtures.EnterpriseAccessTest.doc
    login_as = fixtures.EnterpriseAccessTest.login_as

    def setUp(self):
        fixtures.EnterpriseAccessTest.setUp(self)
        database.save_department("finance", "财务部门")
        self.doc("hr-public", "hr", "public")
        self.doc("ops-shared", "operations")
        database.set_document_access("ops-shared", "operations", "department", ["hr"])
        self.doc("finance-secret", "finance")
        for doc_id, category in (("public-doc", "流程问题"), ("ops-shared", "网络问题"),
                                 ("hr-doc", "硬件问题"), ("hr-public", "流程问题")):
            self.db.documents.update_one({"_id": doc_id}, {"$set": {
                "category": category, "categories": [category]}})

    def get(self, path, **params):
        response = self.api.get(path, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def counts(items):
        return {item["name"]: item["count"] for item in items if item["count"]}

    @staticmethod
    def departments(payload):
        return {item["id"]: item for item in payload["departments"]}

    def add_event(self, doc_id, department="hr", *, date="2026-11-12", category="流程问题",
                  status="unresolved", shared=None, vendor="", visibility="department"):
        self.doc(doc_id, department, visibility)
        self.db.documents.update_one({"_id": doc_id}, {"$set": {
            "doc_type": "record", "summary": f"{date}，事项：资料流程跟进。",
            "uploaded_at": date + " 09:00:00", "category": category, "categories": [category],
            "resolved": status, "shared_department_ids": shared or [], "vendor": vendor,
        }})

    def test_document_filter_keeps_visible_department_options_and_scopes_counts(self):
        self.login_as("hr")
        data = self.get("/api/documents", doc_type="doc", department_id="operations")
        self.assertEqual({doc["id"] for doc in data["documents"]}, {"public-doc", "ops-shared"})
        self.assertEqual(data["stats"]["documents"], 2)
        self.assertEqual(self.counts(data["categories"]), {"流程问题": 1, "网络问题": 1})
        departments = self.departments(data)
        self.assertEqual({key: item["count"] for key, item in departments.items()}, {"operations": 2, "hr": 2})
        self.assertEqual(departments["operations"]["name"], "默认部门")
        self.assertEqual(departments["hr"]["name"], "人事部门")
        self.assertTrue(all(doc["department_name"] == "默认部门" for doc in data["documents"]))

        category = self.get("/api/documents", doc_type="doc", department_id="operations", category="流程问题")
        self.assertEqual([doc["id"] for doc in category["documents"]], ["public-doc"])
        self.assertEqual(category["stats"]["documents"], 2)
        self.assertEqual(self.counts(category["categories"]), {"流程问题": 1, "网络问题": 1})
        self.assertEqual(self.departments(category), departments)

    def test_document_type_has_its_own_department_options_and_statistics(self):
        self.add_event("hr-record", "hr")
        self.add_event("public-record", "operations", visibility="public")
        self.login_as("hr")
        records = self.get("/api/documents", doc_type="record", department_id="operations")
        self.assertEqual([doc["id"] for doc in records["documents"]], ["public-record"])
        self.assertEqual(records["stats"]["documents"], 1)
        self.assertEqual({key: row["count"] for key, row in self.departments(records).items()},
                         {"operations": 1, "hr": 1})
        docs = self.get("/api/documents", doc_type="doc", department_id="operations")
        self.assertEqual({doc["id"] for doc in docs["documents"]}, {"public-doc", "ops-shared"})
        self.assertEqual(docs["stats"]["documents"], 2)
        self.assertEqual({key: row["count"] for key, row in self.departments(docs).items()},
                         {"operations": 2, "hr": 2})

    def test_selecting_an_unreadable_department_returns_no_data_or_counts(self):
        self.login_as("hr")
        docs = self.get("/api/documents", department_id="finance")
        self.assertEqual(docs["documents"], [])
        self.assertEqual(docs["stats"]["documents"], 0)
        self.assertTrue(all(row["count"] == 0 for row in docs["categories"]))
        self.assertNotIn("finance", self.departments(docs))
        self.assertNotIn("finance-secret", str(docs))

        timeline = self.get("/api/timeline", department_id="finance")
        self.assertEqual(timeline["events"], [])
        self.assertEqual(timeline["total"], 0)
        self.assertEqual(timeline["issue_count"], 0)
        self.assertEqual(timeline["categories"], [])
        self.assertEqual(timeline["vendors"], [])
        self.assertTrue(all(value == 0 for value in timeline["status_counts"].values()))
        self.assertNotIn("finance", self.departments(timeline))
        self.assertNotIn("finance-secret", str(timeline))

    def test_timeline_department_filter_runs_before_pagination_and_all_count_facets(self):
        self.db.documents.delete_many({})  # 仅本测试 UUID 库里的合成数据。
        for number in range(35):
            status = ("resolved", "improving", "unresolved")[number % 3]
            self.add_event(f"hr-{number:02}", status=status)
        for number in range(40):
            self.add_event(f"private-{number:02}", "operations", date="2026-11-20")
        for number in range(3):
            self.add_event(f"shared-{number}", "operations", date="2026-11-10",
                           category="厂商系统", shared=["hr"], vendor="供应商甲")
        self.add_event("outside-date", date="2026-10-31")
        self.add_event("finance-secret", "finance", date="2026-11-25")
        database.set_event_status("hr-00", "hr-00", "unresolved")
        database.set_event_status("hr-34", "hr-34", "temporary")
        self.login_as("hr")
        params = {"start_date": "2026-11-01", "end_date": "2026-11-30", "department_id": "hr"}
        first = self.get("/api/timeline", **params)
        last = self.get("/api/timeline", **params, offset=30)
        self.assertEqual([len(first["events"]), len(last["events"])], [30, 5])
        self.assertEqual(first["next_offset"], 30)
        self.assertTrue(first["has_more"])
        self.assertFalse(last["has_more"])
        ids = [event["doc_id"] for page in (first, last) for event in page["events"]]
        self.assertEqual(ids, [f"hr-{number:02}" for number in reversed(range(35))])
        self.assertEqual(len(set(ids)), 35)
        for page in (first, last):
            self.assertEqual(page["total"], 35)
            self.assertEqual(page["issue_count"], 35)
            self.assertEqual(self.counts(page["categories"]), {"流程问题": 35})
            self.assertEqual(page["vendors"], [])
            self.assertEqual(page["status_counts"], {"": 35, "yes": 11, "no": 12, "improving": 11, "temporary": 1})
            self.assertEqual({key: row["count"] for key, row in self.departments(page).items()},
                             {"hr": 35, "operations": 3})
            self.assertTrue(all(event["department_name"] == "人事部门" for event in page["events"]))

        resolved = self.get("/api/timeline", **params, status="yes", limit=2)
        self.assertEqual(resolved["total"], 11)
        self.assertEqual(len(resolved["events"]), 2)
        self.assertEqual(resolved["status_counts"], first["status_counts"])
        self.assertEqual(self.departments(resolved), self.departments(first))

        shared = self.get("/api/timeline", start_date="2026-11-01", end_date="2026-11-30",
                          department_id="operations", category="厂商系统", vendor="供应商甲", status="no")
        self.assertEqual(shared["total"], 3)
        self.assertEqual(shared["issue_count"], 3)
        self.assertEqual(self.counts(shared["categories"]), {"厂商系统": 3})
        self.assertEqual(shared["vendors"], [{"name": "供应商甲", "count": 3}])
        self.assertEqual(shared["status_counts"], {"": 3, "yes": 0, "no": 3, "improving": 0, "temporary": 0})
        self.assertEqual(self.departments(shared), self.departments(first))

    def test_public_department_options_only_count_public_documents_even_with_admin_cookie(self):
        self.search.cookies.set(auth.SESSION_COOKIE, auth.create_session("boss"))
        response = self.search.get("/api/documents", params={"department_id": "operations"})
        self.assertEqual(response.status_code, 200, response.text)
        docs = response.json()
        self.assertEqual([doc["id"] for doc in docs["documents"]], ["public-doc"])
        self.assertEqual(docs["stats"]["documents"], 1)
        self.assertEqual({key: row["count"] for key, row in self.departments(docs).items()},
                         {"operations": 1, "hr": 1})
        self.assertNotIn("finance", self.departments(docs))
        self.assertEqual(docs["documents"][0]["department_name"], "默认部门")

        timeline_response = self.search.get("/api/timeline", params={"department_id": "hr"})
        self.assertEqual(timeline_response.status_code, 200, timeline_response.text)
        timeline = timeline_response.json()
        self.assertEqual([event["doc_id"] for event in timeline["events"]], ["hr-public"])
        self.assertEqual(timeline["total"], 1)
        self.assertEqual({key: row["count"] for key, row in self.departments(timeline).items()},
                         {"operations": 1, "hr": 1})

    def test_generic_timeline_includes_knowledge_documents_in_the_selected_department(self):
        self.db.documents.update_one({"_id": "hr-doc"}, {"$set": {
            "category": "常识库", "categories": ["常识库"]}})
        self.login_as("hr")
        timeline = self.get("/api/timeline", department_id="hr")
        self.assertEqual({event["doc_id"] for event in timeline["events"]}, {"hr-doc", "hr-public"})
        self.assertEqual(timeline["total"], 2)
        self.assertEqual(timeline["event_count"], 2)
        self.assertEqual(timeline["issue_count"], 1)
        self.assertEqual(self.counts(timeline["categories"]), {"常识库": 1, "流程问题": 1})

    def test_department_names_follow_creation_and_rename_without_reindexing(self):
        self.login_as("boss")
        created = self.api.post("/api/departments", json={"id": "legal", "name": "法务部门"})
        self.assertEqual(created.status_code, 200, created.text)
        self.doc("legal-public", "legal", "public")
        self.assertEqual(self.get("/api/documents", department_id="legal")["documents"][0]["department_name"], "法务部门")
        renamed = self.api.put("/api/departments/legal", json={"name": "法务支持"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        docs = self.get("/api/documents", department_id="legal")
        self.assertEqual(docs["documents"][0]["department_name"], "法务支持")
        self.assertEqual(self.departments(docs)["legal"]["name"], "法务支持")
        self.assertEqual(self.get("/api/documents/legal-public")["department_name"], "法务支持")
        preview = self.get("/api/documents/legal-public/preview")
        self.assertEqual(preview["doc"]["department_name"], "法务支持")
        timeline = self.get("/api/timeline", department_id="legal")
        self.assertEqual(timeline["events"][0]["department_name"], "法务支持")
        # 可信内部服务调用与 HTTP 一样使用当前部门名称。
        self.assertEqual(kb.list_documents(department_id="legal")[0]["department_name"], "法务支持")

    def test_invalid_document_type_is_rejected_before_producing_inconsistent_facets(self):
        self.login_as("hr")
        response = self.api.get("/api/documents", params={"doc_type": "invalid"})
        self.assertEqual(response.status_code, 422)

    def test_default_department_rename_is_precise_and_preserves_document_content(self):
        self.db.departments.update_one({"_id": "operations"}, {"$set": {"name": "运维部门"}})
        self.db.departments.insert_one({"_id": "custom", "name": "运维部门"})
        content_path = self.config.raw_dir / "ops-doc.md"
        original = "# 运维部门真实业务文档\n\n原始内容不因页面通用化而改写。"
        content_path.write_text(original, encoding="utf-8")
        database.initialize_enterprise()
        self.assertEqual(self.db.departments.find_one({"_id": "operations"})["name"], "默认部门")
        self.assertEqual(self.db.departments.find_one({"_id": "custom"})["name"], "运维部门")
        self.assertEqual(database.get("ops-doc").department_id, "operations")
        self.assertEqual(content_path.read_text(encoding="utf-8"), original)
        self.db.departments.update_one({"_id": "operations"}, {"$set": {"name": "业务支持部门"}})
        database.initialize_enterprise()
        self.assertEqual(self.db.departments.find_one({"_id": "operations"})["name"], "业务支持部门")


if __name__ == "__main__":
    unittest.main()
