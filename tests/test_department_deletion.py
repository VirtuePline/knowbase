"""部门删除和两级部门角色回归；仅写 UUID 临时库与临时文件。"""
import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.services import auth, database, kb
from tests import test_enterprise_access as fixtures


class DepartmentDeletionTest(unittest.TestCase):
    doc = fixtures.EnterpriseAccessTest.doc
    login_as = fixtures.EnterpriseAccessTest.login_as

    def setUp(self):
        fixtures.EnterpriseAccessTest.setUp(self)
        self.login_as("boss")

    def delete_department(self, department_id, target=None):
        options = {} if target is None else {"json": {"target_department_id": target}}
        return self.api.request("DELETE", f"/api/departments/{department_id}", **options)

    def snapshot(self):
        return {name: list(self.db[name].find({}).sort("_id", 1))
                for name in ("departments", "documents", "users", "app_settings")}

    def test_deletion_preview_counts_documents_members_and_sharing_without_mutating(self):
        database.save_department("finance", "财务部门")
        self.doc("shared-reference", "operations")
        database.set_document_access("shared-reference", "operations", "department", ["hr"])
        before = self.snapshot()

        response = self.api.get("/api/departments/hr/deletion")
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview["department"], {"id": "hr", "name": "人事部门"})
        self.assertEqual(preview["documents"], 1)
        self.assertEqual(preview["members"], 3)
        self.assertEqual(preview["shared_documents"], 1)
        self.assertEqual({item["id"] for item in preview["departments"]}, {"operations", "finance"})
        self.assertEqual(self.snapshot(), before)

    def test_only_platform_admin_can_preview_or_delete_and_public_port_has_no_route(self):
        self.api.cookies.clear()
        self.assertEqual(self.api.get("/api/departments/hr/deletion").status_code, 401)
        self.assertEqual(self.delete_department("hr", "operations").status_code, 401)
        for account in ("hr", "manager"):
            with self.subTest(account=account):
                self.login_as(account)
                self.assertEqual(self.api.get("/api/departments/hr/deletion").status_code, 403)
                self.assertEqual(self.delete_department("hr", "operations").status_code, 403)
        self.search.cookies.set(auth.SESSION_COOKIE, auth.create_session("boss"))
        self.assertIn(self.search.get("/api/departments/hr/deletion").status_code, (404, 405))
        self.assertIn(self.search.request("DELETE", "/api/departments/hr",
                                        json={"target_department_id": "operations"}).status_code, (404, 405))
        self.assertIsNotNone(self.db.departments.find_one({"_id": "hr"}))
        self.assertEqual(database.get("hr-doc").department_id, "hr")

    def test_nonempty_department_requires_an_existing_different_migration_target(self):
        before = self.snapshot()
        self.assertIn(self.delete_department("hr").status_code, (400, 409))
        for target in ("hr", "missing-department"):
            with self.subTest(target=target):
                response = self.delete_department("hr", target)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.snapshot(), before)

    def test_empty_department_can_be_deleted_without_a_body_and_invalid_target_still_fails(self):
        database.save_department("finance", "财务部门")
        for target in ("finance", "missing-department"):
            with self.subTest(target=target):
                self.assertEqual(self.delete_department("finance", target).status_code, 400)
        old_revision = database.access_revision()
        response = self.delete_department("finance")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.db.departments.find_one({"_id": "finance"}))
        self.assertGreater(database.access_revision(), old_revision)
        self.assertEqual(self.api.get("/api/departments/finance/deletion").status_code, 404)
        self.assertEqual(self.delete_department("finance").status_code, 404)

    def test_stale_rename_cannot_recreate_deleted_department_or_create_overwrite_an_existing_one(self):
        database.save_department("finance", "财务部门")
        self.assertEqual(self.delete_department("finance").status_code, 200)
        before = self.snapshot()
        self.assertEqual(self.api.put("/api/departments/finance", json={"name": "旧页面重命名"}).status_code, 404)
        self.assertEqual(self.api.post("/api/departments", json={"id": "hr", "name": "误覆盖名称"}).status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_deletion_migrates_owned_documents_preserves_assets_and_cleans_all_references(self):
        database.save_department("finance", "财务部门")
        self.doc("hr-public", "hr", "public")
        self.doc("finance-doc", "finance")
        database.set_document_access("finance-doc", "finance", "department", ["hr", "operations"])
        database.set_document_access("hr-doc", "hr", "department", ["hr", "operations", "finance"])
        self.db.documents.update_one({"_id": "hr-doc"}, {"$set": {
            "doc_type": "record", "uploaded_by": "hr", "uploaded_by_name": "人事记录人",
            "last_edited_by": "manager", "last_edited_name": "人事主管",
            "last_edited_at": "2026-11-13 09:00:00", "resolved": "improving",
            "attachments": [{"stored": "proof.pdf", "name": "审批附件.pdf", "size": 15}],
        }})
        self.db.users.update_one({"_id": "hr"}, {"$set": {
            "department_ids": ["hr", "operations"],
            "department_roles": {"hr": "editor", "operations": "editor"},
        }})
        # 未列为成员的历史角色引用也必须清理，不能在再次使用同 ID 时恢复权限。
        self.db.users.update_one({"_id": "ops"}, {"$set": {"department_roles.hr": "admin"}})
        before_users = {row["_id"]: copy.deepcopy(row) for row in self.db.users.find({})}
        before_docs = {row["_id"]: copy.deepcopy(row) for row in self.db.documents.find({})}
        source = self.config.raw_dir / "hr-doc.md"
        source_content = source.read_bytes()
        attachment_root = self.root / "attachments"
        attachment_root.mkdir()
        folder = attachment_root / "hr-doc"
        folder.mkdir()
        attachment = folder / "proof.pdf"
        attachment.write_bytes(b"synthetic-proof")
        self.stack.enter_context(patch.object(kb, "settings", replace(self.config, attachments_dir=attachment_root)))
        image = self.config.images_dir / ("c" * 20 + ".png")
        image.write_bytes(b"synthetic-image")
        self.db.image_assets.insert_one({"_id": image.name, "doc_ids": ["hr-doc"], "uploaded_by": "hr"})
        image_metadata = self.db.image_assets.find_one({"_id": image.name})

        old_revision = database.access_revision()
        response = self.delete_department("hr", "operations")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.db.departments.find_one({"_id": "hr"}))
        self.assertGreater(database.access_revision(), old_revision)
        for doc_id, before in before_docs.items():
            after = self.db.documents.find_one({"_id": doc_id})
            self.assertIsNotNone(after)
            expected_department = "operations" if before["department_id"] == "hr" else before["department_id"]
            self.assertEqual(after["department_id"], expected_department)
            self.assertNotIn("hr", after["shared_department_ids"])
            if before["department_id"] == "hr":
                self.assertNotIn("operations", after["shared_department_ids"])
            for field, value in before.items():
                if field not in {"department_id", "shared_department_ids"}:
                    self.assertEqual(after.get(field), value, (doc_id, field))
        self.assertEqual(self.db.documents.find_one({"_id": "hr-doc"})["shared_department_ids"], ["finance"])
        self.assertEqual(self.db.documents.find_one({"_id": "finance-doc"})["shared_department_ids"], ["operations"])
        self.assertEqual(self.db.users.count_documents({}), len(before_users))
        for account, before in before_users.items():
            after = self.db.users.find_one({"_id": account})
            self.assertEqual(after["department_ids"], [item for item in before["department_ids"] if item != "hr"])
            self.assertNotIn("hr", after["department_roles"])
            for field in ("password_hash", "salt", "display_name", "role", "password_version", "login_names"):
                self.assertEqual(after[field], before[field], (account, field))
        self.assertEqual(self.db.users.find_one({"_id": "hr"})["department_roles"], {"operations": "editor"})
        self.assertEqual(source.read_bytes(), source_content)
        self.assertEqual(attachment.read_bytes(), b"synthetic-proof")
        self.assertEqual(image.read_bytes(), b"synthetic-image")
        self.assertEqual(self.db.image_assets.find_one({"_id": image.name}), image_metadata)
        self.assertEqual(self.api.get("/api/documents/hr-doc/attachments/proof.pdf").content, b"synthetic-proof")
        self.assertEqual(self.search.get("/api/documents/hr-public").status_code, 200)
        self.assertEqual(self.search.get("/api/documents/hr-doc").status_code, 404)

    def test_old_sessions_lose_deleted_membership_and_target_members_gain_document_access(self):
        source_token = auth.create_session("manager")
        target_token = auth.create_session("ops")
        self.api.cookies.set(auth.SESSION_COOKIE, source_token)
        before = self.api.get("/api/admin/session").json()
        self.assertEqual(self.api.get("/api/documents/hr-doc").status_code, 200)
        self.login_as("boss")
        self.assertEqual(self.delete_department("hr", "operations").status_code, 200)
        self.api.cookies.set(auth.SESSION_COOKIE, source_token)
        after = self.api.get("/api/admin/session").json()
        self.assertEqual(after["department_ids"], [])
        self.assertEqual(after["department_roles"], {})
        self.assertFalse(after["can_write"])
        self.assertFalse(after["can_manage_users"])
        self.assertNotEqual(after["permission_stamp"], before["permission_stamp"])
        self.assertEqual(self.api.get("/api/documents/hr-doc").status_code, 404)
        self.assertEqual(self.api.get("/api/documents/public-doc").status_code, 200)
        self.api.cookies.set(auth.SESSION_COOKIE, target_token)
        migrated = self.api.get("/api/documents/hr-doc")
        self.assertEqual(migrated.status_code, 200, migrated.text)
        self.assertTrue(migrated.json()["can_write"])

    def test_departments_with_only_members_and_sharing_delete_without_moving_accounts(self):
        database.save_department("finance", "财务部门")
        auth.create_user("finance-user", "Password123", department_ids=["finance"],
                         department_roles={"finance": "editor"})
        database.set_document_access("ops-doc", "operations", "department", ["finance", "hr"])
        response = self.delete_department("finance")
        self.assertEqual(response.status_code, 200, response.text)
        user = self.db.users.find_one({"_id": "finance-user"})
        self.assertEqual(user["department_ids"], [])
        self.assertEqual(user["department_roles"], {})
        self.assertEqual(database.get("ops-doc").shared_department_ids, ["hr"])
        database.initialize_enterprise()
        self.assertEqual(self.db.users.find_one({"_id": "finance-user"})["department_ids"], [])
        database.save_department("finance", "新财务部门")
        self.doc("new-finance", "finance")
        self.login_as("finance-user")
        self.assertEqual(self.api.get("/api/documents/new-finance").status_code, 404)

    def test_default_department_deletion_handles_legacy_data_and_does_not_recreate_it(self):
        self.db.documents.insert_one({"_id": "legacy", "name": "历史资料", "status": "ready"})
        self.db.documents.insert_one({"_id": "empty-owner", "name": "空部门资料", "department_id": ""})
        self.db.documents.insert_one({"_id": "null-owner", "name": "空值部门资料", "department_id": None})
        auth.create_user("legacy-user", "Password123")
        self.db.users.update_one({"_id": "legacy-user"}, {"$unset": {"department_ids": "", "department_roles": ""}})
        response = self.api.get("/api/departments/operations/deletion")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["documents"], 5)
        response = self.delete_department("operations", "hr")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.db.departments.find_one({"_id": "operations"}))
        for doc_id in ("ops-doc", "public-doc", "legacy", "empty-owner", "null-owner"):
            self.assertEqual(self.db.documents.find_one({"_id": doc_id})["department_id"], "hr", doc_id)
        self.assertEqual(self.db.users.find_one({"_id": "legacy-user"})["department_ids"], [])
        self.assertEqual(self.db.users.find_one({"_id": "legacy-user"})["department_roles"], {})
        self.db.documents.insert_one({"_id": "later-legacy", "name": "随后迁入的历史资料"})
        database.initialize_enterprise()
        database.initialize_enterprise()
        self.assertIsNone(self.db.departments.find_one({"_id": "operations"}))
        self.assertEqual({item["id"] for item in database.list_departments()}, {"hr"})
        self.assertEqual(database.get("later-legacy").department_id, "hr")
        self.assertEqual(database.get("later-legacy").visibility, "department")
        self.assertEqual(self.db.users.find_one({"_id": "legacy-user"})["department_ids"], [])
        self.assertEqual(self.db.users.find_one({"_id": "ops"})["department_ids"], [])

    def test_last_department_and_unknown_department_are_rejected_without_changes(self):
        self.assertEqual(self.api.get("/api/departments/unknown/deletion").status_code, 404)
        self.assertEqual(self.delete_department("unknown", "operations").status_code, 404)
        self.assertEqual(self.delete_department("operations", "hr").status_code, 200)
        before = self.snapshot()
        self.assertEqual(self.delete_department("hr").status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_membership_apis_reject_legacy_read_only_role_and_default_to_editor(self):
        response = self.api.post("/api/admin/register", json={
            "account": "newstaff", "username": "新同事", "department_ids": ["hr"],
            "department_roles": {"hr": "member"},
        })
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIsNone(self.db.users.find_one({"_id": "newstaff"}))
        response = self.api.patch("/api/admin/users/hr/departments", json={
            "department_ids": ["hr"], "department_roles": {"hr": "member"},
        })
        self.assertEqual(response.status_code, 400, response.text)
        response = self.api.post("/api/admin/register", json={
            "account": "newstaff", "username": "新同事", "department_ids": ["hr"],
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.db.users.find_one({"_id": "newstaff"})["department_roles"], {"hr": "editor"})
        response = self.api.patch("/api/admin/users/ops/departments", json={"department_ids": ["hr"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["department_roles"], {"hr": "editor"})
        self.assertEqual(self.db.users.find_one({"_id": "ops"})["department_roles"], {"hr": "editor"})


if __name__ == "__main__":
    unittest.main()
