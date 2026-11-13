"""行内引用：索引隔离、保存往返与检索后的模型上下文。"""
import tempfile
import unittest
from tests.network_test_support import online_api_test
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.answer import references, service
from app.core.ingest import pipeline
from app.core.ingest.loaders import load_blocks, reference_ids, _drop_no_ref_lines
from app.domain.schemas import Block, Chunk, ScoredChunk
from app.services.retrieval_service import _embed_text


def hit(refs=None):
    return ScoredChunk(Chunk("c1", "d1", "record.md", "md", "", "交换机丢包",
                             references=refs or []), score=1, coverage=1)


@online_api_test
class InlineReferencesTest(unittest.TestCase):
    def test_reference_is_removed_without_removing_surrounding_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.md"
            content = "# 处理记录\n\n检查[《独特手册》](doc-ref://manual)后重启交换机。"
            path.write_text(content, encoding="utf-8")
            blocks = load_blocks(path)
            self.assertEqual(blocks[0].text, "检查后重启交换机。")
            self.assertEqual(reference_ids(path), ["manual"])
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_heading_and_table_are_cleaned(self):
        ref = "[手册](doc-ref://manual)"
        blocks = _drop_no_ref_lines([Block("", heading="标题" + ref, table=[["名称", ref]])])
        self.assertEqual(blocks[0].heading, "标题")
        self.assertEqual(blocks[0].table, [["名称", ""]])
        self.assertEqual(_drop_no_ref_lines([Block(ref)]), [])

    def test_ingest_stores_relation_without_analyzing_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.md"
            path.write_text("交换机丢包[独特手册](doc-ref://manual)", encoding="utf-8")
            ingester = pipeline.Ingester()
            with patch.object(pipeline, "analyze", return_value=SimpleNamespace(
                    sections=[Block("交换机丢包")], category="", summary="", skipped=0)) as analyze, \
                    patch.object(pipeline, "build_chunks", return_value=[hit().chunk]), \
                    patch.object(ingester, "_write") as write:
                self.assertTrue(ingester.ingest(path, "d1", "record.md", "record").ok)
            self.assertNotIn("独特手册", analyze.call_args.args[0][0].text)
            chunk = write.call_args.args[1][0]
            self.assertEqual(Chunk.from_dict(chunk.to_dict()).references, ["manual"])
            self.assertNotIn("manual", _embed_text(chunk))

    def test_context_deduplicates_and_skips_missing_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manual.md").write_text("# 手册\n\n核对端口速率", encoding="utf-8")
            doc = SimpleNamespace(name="设备手册", stored_name="manual.md", status="ready")
            with patch.object(references, "settings", replace(references.settings, raw_dir=root)), \
                    patch.object(references.database, "get", side_effect=lambda rid: doc if rid == "manual" else None) as get:
                result = references.append_references("【记录 1】交换机丢包", [hit(["manual", "manual", "missing"])])
            self.assertIn("核对端口速率", result)
            self.assertEqual(result.count("的补充引用"), 1)
            self.assertEqual(get.call_count, 2)

    def test_context_respects_budget(self):
        with patch.object(references, "settings", replace(references.settings, max_context_chars=100)), \
                patch.object(references.database, "get") as get:
            context = "【记录 1】" + "正文" * 50
            self.assertEqual(references.append_references(context, [hit(["manual"])]), context)
            get.assert_not_called()

    def test_only_model_path_expands_references_after_retrieval(self):
        config = replace(service.settings, llm_api_key="test-key")
        with patch.object(service, "settings", config), \
                patch.object(service, "uploaded_dates", return_value={}), \
                patch.object(references.database, "get", return_value=None), \
                patch.object(service, "_top_hits", return_value=[hit(["manual"])]) as search, \
                patch.object(service, "extract_solutions", return_value=[]), \
                patch.object(service, "append_references", return_value="【记录 1】补充材料") as expand, \
                patch.object(service, "_draft", return_value=None), \
                patch.object(service, "llm_complete", return_value="核对端口速率") as model:
            service.answer("交换机丢包", fast_mode=False, llm_polish=False)
            expand.assert_not_called()
            model.assert_not_called()
            service.answer("交换机丢包", fast_mode=False, llm_polish=True)
            self.assertEqual(search.call_args.args[0], "交换机丢包")
            self.assertEqual(model.call_args.args[1], "【记录 1】补充材料")
            expand.assert_called_once()


if __name__ == "__main__":
    unittest.main()
