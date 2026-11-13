"""部门成员统一为编辑者；只在 UUID 临时库中验证历史角色迁移。"""
import unittest
from types import SimpleNamespace

from app.services import access, auth, database
from tests import test_enterprise_access as fixtures


class DepartmentRoleIdentityTest(unittest.TestCase):
    def identity(self, roles):
        return auth._row_identity({"_id": "employee", "role": "user", "must_change_password": False,
                                   "department_ids": ["hr"], "department_roles": roles})

    def test_member_and_missing_department_role_become_editor_without_granting_admin(self):
        for stored in ({"hr": "member"}, {}, {"hr": "editor"}):
            with self.subTest(stored=stored):
                identity = self.identity(stored)
                self.assertEqual(identity["department_roles"], {"hr": "editor"})
                self.assertEqual(access.writable_departments(identity), {"hr"})
                self.assertEqual(access.managed_departments(identity), set())

    def test_unknown_role_never_becomes_editor_after_identity_normalization(self):
        for stored in ({"hr": "owner"}, {"hr": None}, {"hr": []}, None):
            with self.subTest(stored=stored):
                identity = self.identity(stored)
                self.assertEqual(identity["department_roles"], {})
                with access.identity_scope(identity):
                    self.assertTrue(access.can_read(SimpleNamespace(department_id="hr", visibility="department")))
                    self.assertFalse(access.can_write(SimpleNamespace(department_id="hr", visibility="department")))

    def test_editor_still_cannot_modify_public_or_cross_department_shared_document(self):
        with access.identity_scope(self.identity({"hr": "member"})):
            self.assertTrue(access.can_write(SimpleNamespace(department_id="hr", visibility="department")))
            self.assertFalse(access.can_write(SimpleNamespace(department_id="hr", visibility="public")))
            shared = SimpleNamespace(department_id="finance", visibility="department", shared_department_ids=["hr"])
            self.assertTrue(access.can_read(shared))
            self.assertFalse(access.can_write(shared))


class DepartmentRoleMigrationTest(unittest.TestCase):
    login_as = fixtures.EnterpriseAccessTest.login_as
    doc = fixtures.EnterpriseAccessTest.doc

    def setUp(self):
        fixtures.EnterpriseAccessTest.setUp(self)

    def test_create_user_defaults_roles_to_the_selected_departments(self):
        auth.create_user("selected", "Password123", department_ids=["hr"])
        self.assertEqual(self.db.users.find_one({"_id": "selected"})["department_roles"], {"hr": "editor"})
        auth.create_user("legacy-member", "Password123", department_ids=["hr"], department_roles={"hr": "member"})
        self.assertEqual(self.db.users.find_one({"_id": "legacy-member"})["department_roles"], {"hr": "editor"})
        auth.create_user("unassigned", "Password123", department_ids=[])
        self.assertEqual(self.db.users.find_one({"_id": "unassigned"})["department_roles"], {})
        with self.assertRaises(ValueError):
            auth.create_user("invalid", "Password123", department_ids=["hr"], department_roles={"hr": "owner"})
        self.assertIsNone(self.db.users.find_one({"_id": "invalid"}))

    def test_existing_session_reads_editor_role_before_startup_migration(self):
        self.db.users.update_one({"_id": "reader"}, {"$set": {"department_roles": {"hr": "member"}}})
        self.login_as("reader")
        response = self.api.get("/api/admin/session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["department_roles"], {"hr": "editor"})
        self.assertTrue(response.json()["can_write"])
        self.assertEqual(self.api.patch("/api/timeline/event-status", json={
            "doc_id": "hr-doc", "chunk_id": "hr-doc", "status": "resolved"}).status_code, 200)
        self.assertEqual(self.db.users.find_one({"_id": "reader"})["department_roles"], {"hr": "member"})

    def test_department_manager_does_not_promote_another_departments_invalid_role(self):
        for invalid in ("owner", None, []):
            with self.subTest(invalid=invalid):
                self.db.users.update_one({"_id": "ops"}, {"$set": {
                    "department_ids": ["operations", "hr"],
                    "department_roles": {"operations": invalid, "hr": "editor"}}})
                self.login_as("manager")
                response = self.api.patch("/api/admin/users/ops/departments", json={
                    "department_ids": ["hr"], "department_roles": {"hr": "editor"}})
                self.assertEqual(response.status_code, 200, response.text)
                saved = self.db.users.find_one({"_id": "ops"})
                self.assertEqual(saved["department_roles"]["operations"], invalid)
                self.assertNotIn("operations", access.writable_departments(auth._row_identity(saved)))
                database.initialize_enterprise()
                self.assertNotIn("operations", access.writable_departments(
                    auth._row_identity(self.db.users.find_one({"_id": "ops"}))))

    def test_migration_is_idempotent_preserves_admin_and_never_promotes_unknown_roles(self):
        self.db.users.update_one({"_id": "reader"}, {"$set": {"department_roles": {"hr": "member"}}})
        self.db.users.update_one({"_id": "hr"}, {"$unset": {"department_roles": ""}})
        self.db.users.update_one({"_id": "ops"}, {"$set": {"department_roles": {"operations": "owner"}}})
        before = database.access_revision()
        database.initialize_enterprise()
        after = database.access_revision()
        self.assertGreater(after, before)
        self.assertEqual(self.db.users.find_one({"_id": "reader"})["department_roles"], {"hr": "editor"})
        self.assertEqual(self.db.users.find_one({"_id": "hr"})["department_roles"], {"hr": "editor"})
        self.assertEqual(self.db.users.find_one({"_id": "manager"})["department_roles"], {"hr": "admin"})
        self.assertEqual(self.db.users.find_one({"_id": "ops"})["department_roles"], {"operations": "owner"})
        self.assertEqual(self.db.users.count_documents({}), 5)
        self.assertEqual(database.get("hr-doc").department_id, "hr")
        database.initialize_enterprise()
        self.assertEqual(database.access_revision(), after)
        invalid = auth._row_identity(self.db.users.find_one({"_id": "ops"}))
        self.assertEqual(access.writable_departments(invalid), set())


if __name__ == "__main__":
    unittest.main()
