import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pymongo import MongoClient

from app.config import settings
from app.core.ingest import analyzer
from app.core.ingest.pipeline import Ingester
from app.domain.schemas import Block
from app.domain.taxonomy import classify_document
from app.services import database, kb
from app.services import timeline as timeline_service

_MONGO_URI = os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017")


@contextmanager
def temp_workspace():
    """把所有 app.* 模块的 settings 指向临时目录与独立测试库，避免写进真实知识库。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mongo_client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")   # 本地没有 MongoDB 时尽早失败
        test_db = f"kbtest_{uuid4().hex[:8]}"
        tmp_settings = replace(
            settings,
            raw_dir=root / "raw",
            index_dir=root / "index",
            chunks_dir=root / "index" / "chunks",
            index_file=root / "index" / "bm25.json",
            images_dir=root / "images",
            analysis_use_llm=False,
            embedding_enabled=False,               # 测试不加载向量模型、不写真实向量库
            mongo_uri=_MONGO_URI,
            mongo_db=test_db,
        )
        for path in (tmp_settings.raw_dir, tmp_settings.chunks_dir, tmp_settings.images_dir):
            path.mkdir(parents=True, exist_ok=True)

        patched = {}
        for name, module in list(sys.modules.items()):
            if name.startswith("app.") and getattr(module, "settings", None) is settings:
                patched[name] = module
                module.settings = tmp_settings
        try:
            database.init_db()
            yield tmp_settings
        finally:
            for module in patched.values():
                module.settings = settings
            mongo_client.drop_database(test_db)
            mongo_client.close()


class RuleCategoryTest(unittest.TestCase):
    def test_mes_document(self):
        self.assertEqual(
            classify_document("MES报工异常处理.md", ["报工提交失败"], ["工单状态异常，接口返回超时"]),
            "MES系统",
        )

    def test_vendor_system_document(self):
        self.assertEqual(
            classify_document("供应商远程支持记录.md", ["第三方系统对接说明"], ["厂商工程师协助处理授权问题"]),
            "厂商系统",
        )

    def test_hardware_document(self):
        self.assertEqual(
            classify_document("贴标机电机异响维修记录.md", ["电机异响"], ["更换轴承后恢复正常"]),
            "硬件问题",
        )

    def test_line_document(self):
        self.assertEqual(
            classify_document("3号线堵料处理记录.md", ["产线节拍异常"], ["流水线工位堆积，调整线速后恢复"]),
            "流水线问题",
        )

    def test_process_document(self):
        self.assertEqual(
            classify_document("点检规范与交接班流程.md", ["点检规范"], ["交接班流程与巡检要求"]),
            "流程问题",
        )

    def test_unrelated_document_falls_back_to_other(self):
        self.assertEqual(classify_document("团队会议纪要.md", ["会议纪要"], ["下季度工作安排"]), "其他")


class AnalyzeCategoryTest(unittest.TestCase):
    def test_analysis_sets_document_category(self):
        blocks = [Block(text="贴标机电机异响，检查轴承并更换后恢复正常。", heading="故障处理")]
        with patch.object(analyzer, "settings", replace(settings, analysis_use_llm=False)):
            result = analyzer.analyze(blocks, "贴标机维修记录.md")

        self.assertEqual(result.category, "硬件问题")

    def test_document_library_analysis_does_not_run_problem_classification(self):
        blocks = [Block(text="员工考勤制度与请假说明。", heading="考勤制度")]
        with patch.object(analyzer, "settings", replace(settings, analysis_use_llm=False)), \
                patch.object(analyzer, "_doc_category") as classify:
            result = analyzer.analyze(blocks, "考勤制度.md", classify_category=False)
        classify.assert_not_called()
        self.assertEqual(result.category, "")
        self.assertTrue(result.sections)


class DocumentCategoryStorageTest(unittest.TestCase):
    def setUp(self):
        self._workspace = temp_workspace()
        self.tmp_settings = self._workspace.__enter__()

    def tearDown(self):
        self._workspace.__exit__(None, None, None)

    def test_category_is_stored_on_document_and_chunks(self):
        result = kb.save_markdown(
            "# MES 报工失败\n\n工单提交提示接口超时，检查 MES 接口日志与账号权限。",
            "",
            "MES报工失败记录.md",
        )
        doc = result["doc"]

        self.assertEqual(doc["category"], "MES系统")
        self.assertEqual([d["id"] for d in kb.list_documents("MES系统")], [doc["id"]])
        self.assertEqual(kb.list_documents("硬件问题"), [])
        self.assertEqual(Ingester().read_chunks(doc["id"])[0].category, "MES系统")

        counts = {item["name"]: item["count"] for item in kb.category_counts()}
        self.assertEqual(counts["MES系统"], 1)
        self.assertEqual(counts["硬件问题"], 0)

    def test_manual_category_correction(self):
        result = kb.save_markdown("# MES 报工失败\n\n报工提交失败排查说明。", "", "记录.md")
        doc_id = result["doc"]["id"]

        updated = kb.set_category(doc_id, "流程问题")
        self.assertEqual(updated["category"], "流程问题")
        self.assertEqual([d["id"] for d in kb.list_documents("流程问题")], [doc_id])

        with self.assertRaises(ValueError):
            kb.set_category(doc_id, "不存在的分类")

    def test_legacy_document_category_is_derived_without_writing_database(self):
        doc = kb.save_markdown("# 设备点检规范\n\n点检与交接班流程要求。", "", "点检规范.md")["doc"]
        database.set_category(doc["id"], "")

        self.assertEqual(kb.list_documents("流程问题")[0]["category"], "流程问题")
        self.assertEqual(database.get(doc["id"]).category, "")


class MultiCategoryTest(unittest.TestCase):
    """一条记录挂多个分类标签：首标签为主分类，筛选与计数按标签展开。"""

    def setUp(self):
        self._workspace = temp_workspace()
        self.tmp_settings = self._workspace.__enter__()

    def tearDown(self):
        self._workspace.__exit__(None, None, None)

    def test_upload_with_multiple_categories(self):
        result = kb.upload(
            "多标签记录.md",
            "# 接口超时\n\nMES 接口超时，同时涉及网络设备问题。".encode("utf-8"),
            doc_type="record", categories="MES系统|网络问题",
        )
        doc = result["doc"]

        self.assertEqual(doc["category"], "MES系统")   # 首标签为主分类
        self.assertEqual(doc["categories"], ["MES系统", "网络问题"])
        # 两个分类下都能筛到同一条记录
        self.assertEqual([d["id"] for d in kb.list_documents("MES系统")], [doc["id"]])
        self.assertEqual([d["id"] for d in kb.list_documents("网络问题")], [doc["id"]])
        self.assertEqual(kb.list_documents("硬件问题"), [])
        # 分类计数按标签展开：一条记录在每个挂的标签下各计一次
        counts = {item["name"]: item["count"] for item in kb.category_counts()}
        self.assertEqual(counts["MES系统"], 1)
        self.assertEqual(counts["网络问题"], 1)
        self.assertEqual(counts["硬件问题"], 0)
        # 时间线事件携带全部分类标签
        events = timeline_service.build()["events"]
        self.assertEqual(events[0]["categories"], ["MES系统", "网络问题"])

    def test_set_category_resets_to_single_tag(self):
        result = kb.upload(
            "重置标签.md", "# 点检流程\n\n点检规范与交接班流程。".encode("utf-8"),
            doc_type="record", categories="MES系统|硬件问题",
        )
        doc_id = result["doc"]["id"]

        updated = kb.set_category(doc_id, "流程问题")

        self.assertEqual(updated["category"], "流程问题")
        self.assertEqual(updated["categories"], ["流程问题"])   # 人工改分类重置为单标签
        self.assertEqual(kb.list_documents("MES系统"), [])
        self.assertEqual([d["id"] for d in kb.list_documents("流程问题")], [doc_id])

    def test_save_markdown_with_multiple_categories(self):
        result = kb.save_markdown(
            "# 接口超时\n\nMES 接口超时，同时涉及网络设备问题。", "", "编辑器多标签.md",
            categories="MES系统|网络问题",
        )
        doc = result["doc"]

        self.assertEqual(doc["category"], "MES系统")   # 首标签为主分类
        self.assertEqual(doc["categories"], ["MES系统", "网络问题"])
        self.assertEqual([d["id"] for d in kb.list_documents("网络问题")], [doc["id"]])
        self.assertEqual([d["id"] for d in kb.list_documents("MES系统")], [doc["id"]])

    def test_set_categories_updates_multiple_tags(self):
        doc = kb.save_markdown("# 点检记录\n\n点检规范执行情况。", "", "改标签.md")["doc"]

        updated = kb.set_categories(doc["id"], ["硬件问题", "流程问题"])

        self.assertEqual(updated["category"], "硬件问题")
        self.assertEqual(updated["categories"], ["硬件问题", "流程问题"])
        self.assertEqual([d["id"] for d in kb.list_documents("流程问题")], [doc["id"]])
        self.assertEqual(kb.list_documents("MES系统"), [])

    def test_single_category_falls_back_for_old_data(self):
        result = kb.upload(
            "旧数据.md", "# 贴标机电机异响\n\n更换轴承后恢复正常。".encode("utf-8"),
            doc_type="record", category="硬件问题",
        )
        doc = result["doc"]

        self.assertEqual(doc["category"], "硬件问题")
        self.assertEqual(doc["categories"], ["硬件问题"])   # 单值分类同步为单标签
        self.assertEqual([d["id"] for d in kb.list_documents("硬件问题")], [doc["id"]])


if __name__ == "__main__":
    unittest.main()
