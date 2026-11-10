import unittest

from app.main import admin_app, search_app


class AppIsolationTest(unittest.TestCase):
    @staticmethod
    def methods(app, path):
        operations = app.openapi()["paths"].get(path, {})
        return {method.upper() for method in operations}

    def test_search_app_has_no_document_write_routes(self):
        writes = [
            ("POST", "/api/documents"),
            ("POST", "/api/images"),
            ("PUT", "/api/documents/{doc_id}/replace"),
            ("GET", "/api/documents/{doc_id}/source"),
            ("POST", "/api/documents/markdown"),
            ("PUT", "/api/documents/{doc_id}"),
            ("PATCH", "/api/documents/{doc_id}/category"),
            ("DELETE", "/api/documents/{doc_id}"),
            ("POST", "/api/documents/rebuild"),
        ]
        for method, path in writes:
            with self.subTest(method=method, path=path):
                self.assertNotIn(method, self.methods(search_app, path))

    def test_admin_app_exposes_management_routes(self):
        writes = [
            ("POST", "/api/documents"),
            ("PUT", "/api/documents/{doc_id}/replace"),
            ("GET", "/api/documents/{doc_id}/source"),
            ("POST", "/api/documents/markdown"),
            ("PUT", "/api/documents/{doc_id}"),
            ("PATCH", "/api/documents/{doc_id}/category"),
            ("DELETE", "/api/documents/{doc_id}"),
            ("POST", "/api/documents/rebuild"),
        ]
        for method, path in writes:
            with self.subTest(method=method, path=path):
                self.assertIn(method, self.methods(admin_app, path))

    def test_admin_app_exposes_image_upload(self):
        self.assertIn("POST", self.methods(admin_app, "/api/images"))

    def test_read_and_download_are_available_on_both_ports(self):
        for app in (search_app, admin_app):
            with self.subTest(app=app.title):
                self.assertIn("POST", self.methods(app, "/api/chat"))
                self.assertIn("GET", self.methods(app, "/api/documents"))
                self.assertIn("GET", self.methods(app, "/api/documents/{doc_id}/preview"))
                self.assertIn("GET", self.methods(app, "/api/documents/{doc_id}/download"))


if __name__ == "__main__":
    unittest.main()
