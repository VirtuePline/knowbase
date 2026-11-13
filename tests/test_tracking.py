"""上传人及运维追踪关系；全部文件与 MongoDB 集合隔离到临时工作区。"""
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from pymongo import MongoClient

from app.config import settings
from app.services import database, kb, mongo, retrieval_service, timeline


_MONGO_URI = os.environ.get("KB_TEST_MONGO", "mongodb://127.0.0.1:27017")
_CONTENT = "# MES 报工异常\n\n故障现象：报工接口超时。\n处理方案：检查接口日志并重新提交。\n"


class TrackingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
        self.addCleanup(self.client.close)
        self.client.admin.command("ping")
        self.db_name = f"kbtest_{uuid4().hex}"
        self.addCleanup(self.client.drop_database, self.db_name)
        self.tmp_settings = replace(
            settings, base_dir=self.root, raw_dir=self.root / "raw",
            index_dir=self.root / "index", chunks_dir=self.root / "index" / "chunks",
            index_file=self.root / "index" / "bm25.json", images_dir=self.root / "images",
            web_dir=self.root / "web", mongo_uri=_MONGO_URI, mongo_db=self.db_name,
            analysis_use_llm=False, embedding_enabled=False, rerank_enabled=False,
            llm_api_key="", vision_api_key="", web_search_enabled=False, web_search_api_key="",
        )
        for directory in (self.tmp_settings.raw_dir, self.tmp_settings.chunks_dir,
                          self.tmp_settings.images_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(mongo, "client", return_value=self.client))
        modules = [module for name, module in list(sys.modules.items())
                   if name.startswith("app.") and module is not None]
        for module in modules:
            if getattr(module, "settings", None) is settings:
                self.stack.enter_context(patch.object(module, "settings", self.tmp_settings))
        original = retrieval_service.retrieval
        self.retrieval = retrieval_service.RetrievalService()
        for module in modules:
            if getattr(module, "retrieval", None) is original:
                self.stack.enter_context(patch.object(module, "retrieval", self.retrieval))

        for module in (kb, retrieval_service, sys.modules["app.core.ingest.pipeline"]):
            self.assertIs(module.settings, self.tmp_settings)
        self.assertIs(mongo.settings, self.tmp_settings)
        for path in (self.tmp_settings.raw_dir, self.tmp_settings.index_dir,
                     self.tmp_settings.chunks_dir, self.tmp_settings.images_dir):
            self.assertTrue(path.resolve().is_relative_to(self.root.resolve()))
        database.init_db()

    def create(self, content=_CONTENT, **kwargs):
        result = kb.save_markdown(content, filename="运维记录.md", **kwargs)
        self.assertEqual(result["doc"]["status"], "ready")
        return result["doc"]

    def test_legacy_document_defaults_new_fields_to_empty(self):
        record = database._row_to_doc({"_id": "legacy", "name": "旧记录", "resolved": "improving"})
        self.assertEqual(record.uploaded_by, "")
        self.assertEqual(record.parent_id, "")
        self.assertEqual(record.root_id, "")
        self.assertEqual(record.doc_type, "record")
        self.assertEqual(record.resolved, "improving")
        self.assertEqual(record.to_dict()["uploaded_by"], "")

    def test_parent_and_root_indexes_are_created(self):
        indexes = mongo.db().documents.index_information()
        keys = [row["key"] for row in indexes.values()]
        self.assertIn([("parent_id", 1)], keys)
        self.assertIn([("root_id", 1)], keys)

    def test_markdown_owner_survives_database_and_preview_roundtrip(self):
        doc = self.create(uploaded_by="alice", resolved="improving")
        stored = database.get(doc["id"])
        self.assertEqual(stored.uploaded_by, "alice")
        self.assertEqual(doc["root_id"], doc["id"])
        self.assertEqual(stored.resolved, "improving")
        self.assertEqual(kb.preview(doc["id"])["doc"]["uploaded_by"], "alice")
        self.assertEqual(kb.list_documents(doc_type="record")[0]["uploaded_by"], "alice")
        self.assertTrue(list(self.tmp_settings.raw_dir.glob(f"{doc['id']}_*")))

    def test_file_upload_records_owner_and_cli_can_leave_owner_empty(self):
        result = kb.upload("维修.md", _CONTENT.encode(), doc_type="record", uploaded_by="bob")
        self.assertEqual(result["doc"]["uploaded_by"], "bob")
        cli_doc = self.create(content=_CONTENT + "\n附加：再次复测。")
        self.assertEqual(cli_doc["uploaded_by"], "")

    def test_nested_tracking_is_independent_and_preserves_root_status(self):
        root = self.create(uploaded_by="alice", resolved="unresolved")
        child = self.create(parent_id=root["id"], uploaded_by="bob", resolved="improving")
        grandchild = self.create(parent_id=child["id"], uploaded_by="carol", resolved="resolved")
        self.assertEqual(len({root["id"], child["id"], grandchild["id"]}), 3)
        self.assertEqual(child["parent_id"], root["id"])
        self.assertEqual(grandchild["parent_id"], child["id"])
        self.assertEqual(child["root_id"], root["id"])
        self.assertEqual(grandchild["root_id"], root["id"])
        self.assertEqual(database.get(root["id"]).resolved, "unresolved")
        self.assertEqual(database.get(root["id"]).uploaded_by, "alice")

    def test_same_parent_owner_and_content_still_create_distinct_tracking(self):
        root = self.create(uploaded_by="alice")
        child = self.create(parent_id=root["id"], uploaded_by="alice")
        other = self.create(parent_id=root["id"], uploaded_by="alice")
        self.assertNotEqual(child["id"], other["id"])
        self.assertEqual(database.count_children(root["id"]), 2)

    def test_tracking_file_upload_forces_record_and_never_deduplicates_parent(self):
        root = self.create(uploaded_by="alice")
        result = kb.upload("追踪.md", _CONTENT.encode(), parent_id=root["id"], uploaded_by="alice")
        self.assertFalse(result["duplicated"])
        self.assertNotEqual(result["doc"]["id"], root["id"])
        self.assertEqual(result["doc"]["doc_type"], "record")
        self.assertEqual(result["doc"]["parent_id"], root["id"])
        self.assertEqual(result["doc"]["root_id"], root["id"])

    def test_equal_content_from_different_owners_is_not_deduplicated(self):
        alice = self.create(uploaded_by="alice")
        bob = self.create(uploaded_by="bob")
        self.assertNotEqual(alice["id"], bob["id"])
        repeated = kb.save_markdown(_CONTENT, filename="别名.md", uploaded_by="alice")
        self.assertTrue(repeated["duplicated"])
        self.assertEqual(repeated["doc"]["id"], alice["id"])
        self.assertEqual(database.get(alice["id"]).uploaded_by, "alice")

    def test_equal_upload_from_different_owners_is_not_deduplicated(self):
        alice = kb.upload("记录.md", _CONTENT.encode(), uploaded_by="alice")["doc"]
        bob = kb.upload("记录.md", _CONTENT.encode(), uploaded_by="bob")["doc"]
        self.assertNotEqual(alice["id"], bob["id"])
        again = kb.upload("记录.md", _CONTENT.encode(), uploaded_by="alice")
        self.assertTrue(again["duplicated"])
        self.assertEqual(again["doc"]["id"], alice["id"])
        self.assertEqual(again["doc"]["uploaded_by"], "alice")

    def test_normal_upload_does_not_reuse_a_tracking_record(self):
        root = self.create(uploaded_by="alice")
        child_content = _CONTENT + "\n补充：已经重新接线。"
        child = self.create(child_content, uploaded_by="alice", parent_id=root["id"])
        standalone = self.create(child_content, uploaded_by="alice")
        self.assertNotEqual(child["id"], standalone["id"])
        self.assertEqual(standalone["parent_id"], "")

    def test_edit_preserves_owner_parent_root_and_original_time(self):
        root = self.create(uploaded_by="alice")
        child = self.create(parent_id=root["id"], uploaded_by="bob", resolved="improving")
        sibling = self.create(parent_id=root["id"], uploaded_by="bob")
        edited = kb.save_markdown(_CONTENT, child["id"], "编辑后的记录.md", uploaded_by="mallory")
        self.assertFalse(edited["duplicated"])
        self.assertEqual(edited["doc"]["id"], child["id"])
        for name in ("uploaded_by", "parent_id", "root_id", "uploaded_at", "resolved", "doc_type"):
            self.assertEqual(edited["doc"][name], child[name])
        self.assertNotEqual(edited["doc"]["id"], sibling["id"])

    def test_edit_cannot_reparent_even_to_current_parent(self):
        root = self.create()
        child = self.create(parent_id=root["id"])
        with self.assertRaisesRegex(ValueError, "不可修改"):
            kb.save_markdown(_CONTENT, child["id"], parent_id=root["id"])
        self.assertEqual(database.get(child["id"]).parent_id, root["id"])

    def test_replacement_preserves_all_creation_metadata(self):
        root = self.create(uploaded_by="alice")
        child = self.create(parent_id=root["id"], uploaded_by="bob", resolved="improving")
        database.set_category(child["id"], "厂商系统")
        database.set_vendor(child["id"], "测试厂商")
        before = database.get(child["id"])
        result = kb.replace_file(child["id"], "修订后的追踪.md",
                                 (_CONTENT + "\n补充处理：更新客户端后恢复。").encode())
        after = database.get(child["id"])
        self.assertTrue(result["replaced"])
        for name in ("id", "uploaded_by", "parent_id", "root_id", "uploaded_at", "resolved",
                     "doc_type", "vendor", "category"):
            self.assertEqual(getattr(after, name), getattr(before, name), name)
        self.assertNotEqual(after.sha256, before.sha256)
        self.assertEqual(after.status, "ready")

    def test_replacement_preserves_basic_document_type_and_owner(self):
        before = kb.upload("说明.md", _CONTENT.encode(), uploaded_by="alice")["doc"]
        after = kb.replace_file(before["id"], "说明修订.md", (_CONTENT + "\n修订：检查端口。").encode())["doc"]
        self.assertEqual(after["doc_type"], "doc")
        self.assertEqual(after["uploaded_by"], "alice")
        self.assertEqual(after["root_id"], before["root_id"])

    def test_tracking_parent_must_exist_and_be_ready_record(self):
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.create(parent_id="missing")
        basic = kb.upload("说明.md", _CONTENT.encode())["doc"]
        with self.assertRaisesRegex(ValueError, "工作记录"):
            self.create(parent_id=basic["id"])
        root = self.create(_CONTENT + "\n补充：状态校验。")
        for status in ("processing", "failed"):
            database.update_status(root["id"], 0, status)
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "完成入库"):
                self.create(parent_id=root["id"])

    def test_legacy_record_can_be_tracked_and_legacy_owner_deduplicates(self):
        root = self.create()
        mongo.db().documents.update_one({"_id": root["id"]}, {"$unset": {
            "doc_type": "", "root_id": "", "parent_id": "", "uploaded_by": ""}})
        duplicate = kb.save_markdown(_CONTENT)
        self.assertTrue(duplicate["duplicated"])
        self.assertEqual(duplicate["doc"]["id"], root["id"])
        child = self.create(parent_id=root["id"])
        self.assertEqual(child["root_id"], root["id"])

    def test_new_tracking_rejects_self_reference_before_writing(self):
        root = self.create()
        with patch.object(kb.uuid, "uuid4", return_value=SimpleNamespace(hex=root["id"] + "0" * 20)):
            with self.assertRaisesRegex(ValueError, "自身"):
                kb.save_markdown(_CONTENT, parent_id=root["id"])
        self.assertEqual(len(database.list_all()), 1)
        self.assertEqual(database.get(root["id"]).parent_id, "")

    def test_delete_parent_is_refused_before_removing_files(self):
        root = self.create()
        child = self.create(parent_id=root["id"])
        source_path = kb.source_file(root["id"])[0]
        chunks_path = self.tmp_settings.chunks_dir / f"{root['id']}.json"
        with self.assertRaisesRegex(ValueError, "后续追踪"):
            kb.delete(root["id"])
        self.assertTrue(source_path.is_file())
        self.assertTrue(chunks_path.is_file())
        self.assertIsNotNone(database.get(root["id"]))
        self.assertTrue(kb.delete(child["id"]))
        self.assertTrue(kb.delete(root["id"]))

    def test_failed_child_still_prevents_parent_deletion(self):
        root = self.create()
        child = self.create(parent_id=root["id"])
        database.update_status(child["id"], 0, "failed")
        with self.assertRaisesRegex(ValueError, "后续追踪"):
            kb.delete(root["id"])

    def test_direct_database_delete_keeps_parent_and_event_status(self):
        root = self.create()
        self.create(parent_id=root["id"])
        database.set_event_status(root["id"], root["id"], "improving")
        with self.assertRaises(ValueError):
            database.delete(root["id"])
        self.assertIsNotNone(database.get(root["id"]))
        self.assertEqual(database.get_event_status_map()[root["id"]], "improving")

    def test_timeline_adds_relationships_without_changing_date_order_or_parent_status(self):
        root = self.create(uploaded_by="alice", resolved="unresolved")
        child = self.create(parent_id=root["id"], uploaded_by="bob", resolved="resolved")
        grandchild = self.create(parent_id=child["id"], uploaded_by="carol", resolved="improving")
        for doc, day in ((root, "01"), (child, "03"), (grandchild, "02")):
            mongo.db().documents.update_one({"_id": doc["id"]}, {"$set": {
                "summary": f"2026年11月{day}日，现象：报工失败。处理：检查接口日志。",
                "uploaded_at": f"2026-11-{day} 08:00:00"}})
        output = timeline.build()
        events = output["events"]
        self.assertEqual([event["doc_id"] for event in events], [child["id"], grandchild["id"], root["id"]])
        by_id = {event["doc_id"]: event for event in events}
        self.assertEqual(by_id[root["id"]]["child_count"], 1)
        self.assertEqual(by_id[child["id"]]["child_count"], 1)
        self.assertEqual(by_id[grandchild["id"]]["child_count"], 0)
        self.assertEqual(by_id[child["id"]]["parent_name"], root["name"])
        self.assertEqual(by_id[child["id"]]["uploaded_by"], "bob")
        self.assertEqual(by_id[child["id"]]["doc_type"], "record")
        self.assertEqual(by_id[grandchild["id"]]["parent_id"], child["id"])
        self.assertEqual(by_id[grandchild["id"]]["root_id"], root["id"])
        self.assertEqual(by_id[root["id"]]["status"], "unresolved")
        self.assertEqual(by_id[child["id"]]["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
