"""模板：默认与已保存模板均不再含「结果（是否解决）」行。"""
import sys
import unittest
from unittest.mock import patch

from app.services import database, recordmeta


class TemplateSolveLineTest(unittest.TestCase):
    def test_default_templates_drop_solve_line(self):
        for name, tpl in recordmeta.DEFAULT_TEMPLATES.items():
            self.assertNotIn("是否解决", tpl, f"{name} 模板不应再含是否解决字段")
            self.assertTrue(tpl.endswith("\n"))

    def test_saved_custom_template_is_filtered_on_read(self):
        saved = {"MES系统": "## MES系统问题处理\n\n- 系统 / 模块：\n- 结果（是否解决）：已解决\n- 原因：\n\n"}
        with patch.object(database, "list_templates", return_value=saved):
            tpl = recordmeta.get_templates()["MES系统"]
        self.assertNotIn("是否解决", tpl)
        self.assertIn("- 系统 / 模块：", tpl)
        self.assertIn("- 原因：", tpl)
        self.assertNotIn("\n\n\n", tpl, "删除字段后不应残留连续空行")


if __name__ == "__main__":
    unittest.main()
