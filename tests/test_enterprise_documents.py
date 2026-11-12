"""部门文档服务与衍生数据权限；真实聚合只操作 UUID 临时测试库。"""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from pymongo import MongoClient

from app.api import routes
from app.domain.schemas import DocRecord
from app.services import access, database, kb, mongo, timeline, weekly


def actor(role="editor", department="operations", account="operator"):
    return {"username": account, "role": "user", "department_ids": [department],
            "department_roles": {department: role}, "must_change_password": False}


class EnterpriseDocumentsTest(unittest.TestCase):
    def setUp(self):
        self.client = MongoClient(os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017"),
                                  serverSelectionTimeoutMS=5000)
        self.addCleanup(self.client.close)
        self.client.admin.command("ping")
        self.name = "kbtest_enterprise_docs_" + uuid4().hex
        self.addCleanup(self.client.drop_database, self.name)
        self.db = self.client[self.name]
        patcher = patch.object(mongo, "db", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        for department in ("operations", "hr", "finance"):
            self.db.departments.insert_one({"_id": department, "name": department})

    def add(self, doc_id, department="operations", visibility="department", **fields):
        doc = DocRecord(id=doc_id, name=doc_id, stored_name=doc_id + ".md", ext=".md", size=20,
                        sha256=doc_id, chunks=1, uploaded_at="2026-11-12 09:00:00", status="ready",
                        summary="2026年11月12日，现象：处理知识资料。", category="流程问题",
                        doc_type="record", department_id=department, visibility=visibility, **fields)
        database.add(doc)
        return doc

    def test_lists_counts_and_write_controls_use_the_same_department_scope(self):
        self.add("own")
        self.add("secret", "hr")
        self.add("public", "hr", "public")
        self.add("shared", "hr", shared_department_ids=["operations"])
        with access.identity_scope(actor("member")):
            self.assertEqual({item["id"] for item in kb.list_documents()}, {"own", "public", "shared"})
            self.assertEqual(kb.stats()["documents"], 3)
            self.assertEqual(next(item["count"] for item in kb.category_counts()
                                  if item["name"] == "流程问题"), 3)
            with self.assertRaises(kb.DocumentNotFound):
                kb.get_document("secret")
            for doc_id in ("own", "public", "shared"):
                self.assertFalse(kb.get_document(doc_id)["can_write"])
                with self.assertRaises(HTTPException) as error:
                    kb.set_categories(doc_id, ["网络问题"])
                self.assertEqual(error.exception.status_code, 403)
        with access.identity_scope(actor()):
            self.assertTrue(kb.get_document("own")["can_write"])
            self.assertFalse(kb.get_document("shared")["can_write"])
            kb.set_categories("own", ["网络问题"])

    def test_permission_changes_require_owner_department_admin_and_public_is_platform_owned(self):
        self.add("own")
        with access.identity_scope(actor()):
            with self.assertRaises(HTTPException) as error:
                routes.update_document_access("own", routes.DocumentAccessRequest(shared_department_ids=["hr"]))
            self.assertEqual(error.exception.status_code, 403)
        with access.identity_scope(actor("admin")):
            result = routes.update_document_access("own", routes.DocumentAccessRequest(shared_department_ids=["hr"]))
            self.assertEqual(result["doc"]["shared_department_ids"], ["hr"])
            with self.assertRaises(HTTPException):
                routes.update_document_access("own", routes.DocumentAccessRequest(visibility="public"))
            with self.assertRaises(HTTPException):
                routes.update_document_access("own", routes.DocumentAccessRequest(department_id="finance"))
        with access.identity_scope({"username": "admin", "role": "admin"}):
            routes.update_document_access("own", routes.DocumentAccessRequest(visibility="public"))
        with access.identity_scope(actor("admin")):
            self.assertFalse(kb.get_document("own")["can_write"])
            with self.assertRaises(HTTPException):
                routes.update_document_access("own", routes.DocumentAccessRequest(visibility="department"))

    def test_timeline_does_not_leak_hidden_parent_names_root_ids_or_child_counts(self):
        self.add("hr-root", "hr")
        self.add("own", parent_id="hr-root", root_id="hr-root")
        self.add("hidden-child", "hr", parent_id="own", root_id="hr-root")
        self.add("visible-child", parent_id="own", root_id="own")
        with access.identity_scope(actor("member")):
            result = timeline.build()
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["issue_count"], 2)
        event = next(item for item in result["events"] if item["doc_id"] == "own")
        self.assertEqual(event["parent_name"], "")
        self.assertEqual(event["parent_id"], "")
        self.assertEqual(event["root_id"], "own")
        self.assertEqual(event["child_count"], 1)
        self.assertFalse(event["can_write"])

    def test_status_updates_reject_mismatched_ids_and_inaccessible_records(self):
        self.add("own")
        self.add("secret", "hr")
        with access.identity_scope(actor()):
            for payload, code in (({"doc_id": "own", "chunk_id": "secret"}, 400),
                                  ({"doc_id": "secret", "chunk_id": "secret"}, 404)):
                with self.assertRaises(HTTPException) as error:
                    routes.update_event_status(routes.EventStatusRequest(**payload, status="resolved"))
                self.assertEqual(error.exception.status_code, code)
            routes.update_event_status(routes.EventStatusRequest(doc_id="own", chunk_id="own", status="resolved"))
        self.assertEqual(database.get_event_status_map(), {"own": "resolved"})

    def test_bound_images_require_current_document_access_even_for_the_original_uploader(self):
        self.add("secret", "hr")
        self.db.image_assets.insert_one({"_id": "secret.png", "uploaded_by": "operator", "doc_ids": ["secret"]})
        self.db.image_assets.insert_one({"_id": "draft.png", "uploaded_by": "operator", "doc_ids": []})
        with access.identity_scope(actor()):
            with self.assertRaises(HTTPException) as error:
                kb._validate_images("![image](/images/secret.png)")
            self.assertEqual(error.exception.status_code, 403)
            self.assertEqual(kb._validate_images("![image](/images/draft.png)"), {"draft.png"})

    def test_weekly_reports_use_accounts_and_hide_revoked_source_summaries(self):
        self.add("own", uploaded_by="operator", uploaded_by_name="同名员工")
        self.add("other", "hr", uploaded_by="other", uploaded_by_name="同名员工")
        with access.identity_scope(actor("member")):
            own = weekly.collect_records("2026-11-09", "operator")
            self.assertEqual([item["doc_id"] for item in own], ["own"])
            self.assertEqual(weekly.collect_records("2026-11-09", "同名员工"), [])
        database.save_weekly_report("2026-11-09", "operator", own, "2026-11-12", markdown="安全摘要")
        database.save_weekly_report("2026-11-02", "operator", [{"doc_id": "other"}], "2026-11-12", markdown="私有摘要")
        with access.identity_scope(actor("member")):
            self.assertEqual([item["markdown"] for item in weekly.visible_reports()], ["安全摘要"])
        database.set_document_access("own", "hr", "department", [])
        with access.identity_scope(actor("member")):
            self.assertEqual(weekly.visible_reports(), [])


if __name__ == "__main__":
    unittest.main()
