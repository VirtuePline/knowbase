"""引用文档嵌套在所属命中来源下；全部使用内存替身，不连接数据库或模型。"""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from pymongo.errors import PyMongoError

from app.core.answer import references, service
from app.domain.schemas import Chunk, DocRecord, ScoredChunk, SourceRef


def hit(doc_id="d1", name="交换机记录.md", refs=None, text="交换机丢包", **kwargs):
    return ScoredChunk(Chunk(
        id=doc_id + "-chunk", doc_id=doc_id, doc_name=name, doc_type="md",
        location="章节一", text=text, references=refs or [], **kwargs,
    ), score=1.0, coverage=1.0)


def document(doc_id="manual", name="设备手册.md", **kwargs):
    return DocRecord(
        id=doc_id, name=name, stored_name=doc_id + ".md", ext=".md",
        size=100, sha256="", chunks=1, uploaded_at="2026-11-11 10:30:00",
        status="ready", summary="设备连接和配对说明", **kwargs,
    )


class ReferenceSourcesTest(unittest.TestCase):
    def test_shared_reference_is_nested_once_under_each_parent(self):
        records = [
            hit(refs=["d1", "manual", "manual", ""]),
            hit("d2", "另一条记录.md", ["manual"]),
            hit(refs=["manual"]),
        ]
        existing = [SourceRef("d1", "交换机记录.md", "章节一", 1.0, ""),
                    SourceRef("d2", "另一条记录.md", "章节一", 0.8, "")]
        with patch.object(references.database, "get", return_value=document()) as get, \
                patch.object(references, "load_blocks") as load:
            sources = references.attach_reference_sources(records, existing)
        get.assert_called_once_with("manual")
        load.assert_not_called()  # 只添加来源，不读取正文、嵌套引用或执行文档里的指令
        self.assertIs(sources, existing)
        self.assertEqual([source.doc_id for source in sources], ["d1", "d2"])
        self.assertEqual([[ref.doc_id for ref in source.references] for source in sources],
                         [["manual"], ["manual"]])
        reference = sources[0].references[0]
        self.assertEqual(reference.summary, "设备连接和配对说明")
        self.assertEqual(reference.uploaded_at, "2026-11-11 10:30:00")
        serialized = reference.to_dict()
        self.assertEqual(serialized["kind"], "knowledge_base")
        self.assertEqual(serialized["scoring_method"], "reference")
        self.assertEqual(serialized["references"], [])
        for metric in ("rrf_score", "keyword_coverage", "semantic_similarity", "rerank_score"):
            self.assertIsNone(serialized[metric])

    def test_equal_document_names_do_not_attach_references_to_the_wrong_parent(self):
        records = [hit("d1", "同名记录.md", ["manual-a"]),
                   hit("d2", "同名记录.md", ["manual-b"])]
        existing = [SourceRef(record.chunk.doc_id, record.chunk.doc_name, "章节一", 1.0, "")
                    for record in records]
        docs = {doc_id: document(doc_id) for doc_id in ("manual-a", "manual-b")}
        with patch.object(references.database, "get", side_effect=docs.get):
            sources = references.attach_reference_sources(records, existing)
        self.assertEqual([[ref.doc_id for ref in source.references] for source in sources],
                         [["manual-a"], ["manual-b"]])

    def test_missing_unready_and_unavailable_references_do_not_hide_valid_sources(self):
        docs = {
            "missing": None,
            "processing": replace(document(), status="processing"),
            "failed": replace(document(), status="failed"),
            "manual": document(),
        }

        def get_document(doc_id):
            if doc_id == "unavailable":
                raise PyMongoError("metadata unavailable")
            return docs[doc_id]

        refs = ["missing", "processing", "failed", "unavailable", "manual"]
        existing = [SourceRef("d1", "交换机记录.md", "章节一", 1.0, ""),
                    SourceRef("d2", "另一条记录.md", "章节一", 0.8, "")]
        with patch.object(references.database, "get", side_effect=get_document) as get:
            sources = references.attach_reference_sources([
                hit(refs=refs), hit("d2", "另一条记录.md", refs),
            ], existing)
        self.assertEqual([source.doc_id for source in sources], ["d1", "d2"])
        self.assertEqual([[ref.doc_id for ref in source.references] for source in sources],
                         [["manual"], ["manual"]])
        self.assertEqual(get.call_count, 5)

    def test_direct_hits_keep_scores_and_mutual_references_do_not_form_a_cycle(self):
        records = [hit(refs=["manual"]), hit("manual", "设备手册.md", ["d1"])]
        with patch.object(service, "uploaded_dates", return_value={}), \
                patch.object(references.database, "get") as get:
            sources = service._sources(records, "交换机丢包")
        self.assertEqual([source.doc_id for source in sources], ["d1", "manual"])
        self.assertEqual(sources[1].scoring_method, "rrf")
        self.assertEqual(sources[1].rrf_score, records[1].score)
        self.assertEqual([[ref.doc_id for ref in source.references] for source in sources],
                         [["manual"], ["d1"]])
        for source in sources:
            reference = source.references[0]
            self.assertEqual(reference.scoring_method, "reference")
            self.assertIsNone(reference.rrf_score)
            self.assertEqual(reference.references, [])
            self.assertEqual(source.to_dict()["references"][0]["references"], [])
        get.assert_not_called()


class ReferenceSourcesFlowTest(unittest.TestCase):
    def setUp(self):
        config = replace(service.settings, llm_api_key="test-key")
        patches = [
            patch.object(service, "settings", config),
            patch.object(service, "uploaded_dates", return_value={"d1": "2026-11-10 09:00:00"}),
            patch.object(references.database, "get", return_value=document()),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_rule_solution_adds_its_reference_in_fast_and_default_modes(self):
        record = hit(name="扫码枪扫不上", refs=["manual"], text=(
            "故障现象：扫码枪扫不上\n处理方案：扫描底座蓝牙二维码即可\n处理结果：解决"
        ))
        unused = hit("d2", "另一条扫码枪记录", ["unused"], text="扫码枪扫不上，等待处理")
        for fast in (True, False):
            with self.subTest(fast_mode=fast), \
                    patch.object(service, "_top_hits", return_value=[record, unused]), \
                    patch.object(service, "append_references") as expand, \
                    patch.object(service, "draft_solution") as model:
                response = service.answer("扫码枪扫不上", fast_mode=fast)
            self.assertTrue(response.solution_found)
            self.assertFalse(response.llm_used)
            self.assertIn("扫描底座蓝牙二维码即可", response.answer)
            self.assertNotIn("来源", response.answer)
            self.assertNotIn(record.chunk.doc_name, response.answer)
            self.assertEqual([source.doc_id for source in response.sources], ["d1"])
            self.assertEqual([ref.doc_id for ref in response.sources[0].references], ["manual"])
            self.assertEqual(response.sources[0].references[0].scoring_method, "reference")
            expand.assert_not_called()
            model.assert_not_called()

    def test_raw_excerpt_also_lists_reference_without_loading_attachment(self):
        with patch.object(service, "_top_hits", return_value=[hit(refs=["manual"])]), \
                patch.object(references, "load_blocks") as load:
            response = service.answer("交换机丢包", fast_mode=True)
        self.assertEqual(response.status, "not_found")
        self.assertIn("交换机丢包", response.answer)
        self.assertNotIn("交换机记录.md", response.answer)
        self.assertNotIn("来源", response.answer)
        self.assertEqual([source.doc_id for source in response.sources], ["d1"])
        self.assertEqual([ref.doc_id for ref in response.sources[0].references], ["manual"])
        load.assert_not_called()

    def test_model_plan_only_adds_references_for_selected_records(self):
        records = [hit(refs=["unused"]), hit("d2", "另一条交换机记录.md", ["manual"])]
        with patch.object(service, "_top_hits", return_value=records), \
                patch.object(service, "append_references", side_effect=lambda context, hits: context), \
                patch.object(service, "draft_solution", return_value=service.SolutionDraft(
                    ["核对端口速率。"], [2])), \
                patch.object(references.database, "get", return_value=document()) as get:
            response = service.answer("交换机丢包", fast_mode=False, llm_polish=True)
        self.assertTrue(response.llm_used)
        self.assertEqual([source.doc_id for source in response.sources], ["d2"])
        self.assertEqual([ref.doc_id for ref in response.sources[0].references], ["manual"])
        self.assertNotIn("来源", response.answer)
        self.assertNotIn("交换机记录.md", response.answer)
        self.assertNotIn("设备手册.md", response.answer)
        get.assert_called_once_with("manual")

    def test_model_qa_nests_shared_reference_under_each_selected_record(self):
        records = [hit(refs=["manual"]), hit("d2", "另一条交换机记录.md", ["manual"])]
        with patch.object(service, "_top_hits", return_value=records), \
                patch.object(service, "append_references", side_effect=lambda context, hits: context), \
                patch.object(service, "draft_solution", return_value=None), \
                patch.object(service, "llm_complete", return_value="交换机记录显示端口速率不一致。"):
            response = service.answer("交换机丢包", fast_mode=False, llm_polish=True)
        self.assertTrue(response.llm_used)
        self.assertFalse(response.solution_found)
        self.assertEqual([source.doc_id for source in response.sources], ["d1", "d2"])
        self.assertEqual([[ref.doc_id for ref in source.references] for source in response.sources],
                         [["manual"], ["manual"]])

    def test_full_ledger_statistics_include_anchor_reference(self):
        anchor = hit(refs=["manual"], text="", heading="设备台账", table=[
            ["设备", "IP"], ["主机1", "10.10.0.1"],
        ])
        second = replace(anchor.chunk, id="second", table=[
            ["设备", "IP"], ["主机2", ""],
        ])
        fake_retrieval = SimpleNamespace(index=SimpleNamespace(chunks=[anchor.chunk, second]))
        with patch.object(service, "_top_hits", return_value=[anchor]), \
                patch.object(service, "retrieval", fake_retrieval), \
                patch.object(service, "append_references", side_effect=lambda context, hits: context), \
                patch.object(service, "llm_complete", return_value="1 台设备未登记 IP。"):
            response = service.answer("有几台设备没有IP", fast_mode=False, llm_polish=True)
        self.assertTrue(response.llm_used)
        self.assertIn("完整台账", response.notice)
        self.assertEqual([source.doc_id for source in response.sources], ["d1", "d1"])
        self.assertEqual([[ref.doc_id for ref in source.references] for source in response.sources],
                         [["manual"], ["manual"]])


if __name__ == "__main__":
    unittest.main()
