import re
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.core.ingest import analyzer, pipeline
from app.core.ingest.analyzer import analyze
from app.core.ingest.chunker import build_chunks
from app.core.ingest.pipeline import Ingester
from app.domain.schemas import Block


class AnalyzerTest(unittest.TestCase):
    def test_noise_is_skipped_and_main_section_is_summarized(self):
        blocks = [
            Block(heading="目录", text="第一章 ...... 1\n第二章 ...... 2\n第三章 ...... 3"),
            Block(heading="故障处理", text="E-1024 报警时检查伺服驱动器接头，重新插紧后复测。", location="第3页"),
        ]
        with patch.object(analyzer, "settings", replace(settings, analysis_use_llm=False)):
            result = analyze(blocks, "维修手册.md")

        self.assertEqual(result.skipped, 1)
        self.assertEqual(len(result.sections), 1)
        self.assertIn("E-1024", result.sections[0].summary)
        self.assertEqual(result.sections[0].text, blocks[1].text)
        self.assertTrue(result.summary)
        self.assertEqual(blocks[1].summary, "", "分析不应修改解析器返回的原始块")

    def test_summary_prevents_full_text_chunking_and_preserves_source(self):
        text = "贴标机出现标签偏移。" + "检查电眼位置并清洁光纤，处理后需要重新校准。" * 35
        local = replace(
            settings, analysis_use_llm=False, analysis_max_section_chars=2000,
            chunk_size=200, chunk_overlap=20,
        )
        with patch.object(analyzer, "settings", local), patch("app.core.ingest.chunker.settings", local):
            result = analyze([Block(text=text, heading="贴标机")], "手册.md")
            chunks = build_chunks(result.sections, {"id": "d1", "name": "手册.md", "type": "md"})

        self.assertEqual(len(result.sections), 1)
        self.assertEqual(len(chunks), 1, "已分析章节按摘要建立检索单元，不再按全文细切")
        self.assertGreater(len(chunks[0].text), local.chunk_size)
        self.assertEqual(chunks[0].text, text)
        self.assertTrue(chunks[0].summary)

    def test_pdf_pages_are_merged_by_document_structure(self):
        blocks = [
            Block(text="设备维护手册\n第一章 概述\n本手册说明设备维护的基本流程。", location="第1页"),
            Block(text="第二章 故障处理\n发生报警时先停机并确认安全。\n2.1 驱动器故障\n检查驱动器接头和供电。", location="第2页"),
            Block(text="2.1 驱动器故障\n恢复供电后执行试运行并确认报警消除。", location="第3页"),
        ]
        with patch.object(analyzer, "settings", replace(settings, analysis_use_llm=False)):
            result = analyze(blocks, "维护手册.pdf")

        headings = [section.heading for section in result.sections]
        self.assertIn("第一章 概述", headings)
        self.assertIn("第二章 故障处理", headings)
        self.assertEqual(headings.count("2.1 驱动器故障"), 1, "跨页重复标题不能拆成多个摘要")
        self.assertTrue(any(section.location == "第2-3页" for section in result.sections))
        self.assertFalse(any(re.fullmatch(r"第\d+页", heading) for heading in headings))
        joined = "\n".join(section.text for section in result.sections)
        self.assertIn("恢复供电后执行试运行", joined)

    def test_pdf_outline_titles_define_sections_across_pages(self):
        blocks = [
            Block(text="确认工具、备件和现场安全条件。", location="第1页", heading="安装准备"),
            Block(text="清点物料并在设备断电后完成机械安装。", location="第2页"),
            Block(text="依据报警类别检查传感器和接线。", location="第3页", heading="故障处理"),
        ]
        with patch.object(analyzer, "settings", replace(settings, analysis_use_llm=False)):
            result = analyze(blocks, "设备手册.pdf")

        self.assertEqual([section.heading for section in result.sections], ["安装准备", "故障处理"])
        self.assertEqual([section.location for section in result.sections], ["第1-2页", "第3页"])

    def test_pdf_without_headings_is_not_summarized_per_page(self):
        blocks = [
            Block(text="设备启动前应确认防护门已经关闭。", location="第1页"),
            Block(text="运行过程中需要观察电机温度和异常噪声。", location="第2页"),
            Block(text="停机后按照点检表完成清洁和状态记录。", location="第3页"),
        ]
        with patch.object(analyzer, "settings", replace(settings, analysis_use_llm=False)):
            result = analyze(blocks, "运行说明.pdf")

        self.assertEqual(len(result.sections), 1, "无标题时应形成一个自然结构段，而不是按页拆分")
        self.assertEqual(result.sections[0].location, "第1-3页", "页码仅作为溯源元数据")
        self.assertNotIn("第1页", result.summary)
        self.assertNotIn("第3页", result.summary)
        self.assertIn("防护门", result.sections[0].summary)
        self.assertIn("状态记录", result.sections[0].summary)

    def test_summary_keeps_time_but_omits_people_and_record_numbers(self):
        block = Block(
            text="- 时间：2025-05-22 11:20\n- 运维人：刘工\n- 工单号：WX-001\n"
                 "- 处理：先执行安全复位，再检查门锁回路。\n- 结果：恢复生产。",
            heading="故障处理",
        )
        with patch.object(analyzer, "settings", replace(settings, analysis_use_llm=False)):
            result = analyze([block], "维修记录.md")

        summary = result.sections[0].summary
        self.assertIn("2025-05-22", summary, "摘要应保留事件发生时间")
        self.assertNotIn("刘工", summary)
        self.assertNotIn("WX-001", summary)
        self.assertIn("检查门锁回路", summary)

    def test_llm_input_contains_structure_but_not_page_location(self):
        captured = {}

        def fake_chat(system, user):
            captured["user"] = user
            return '{"sections": [{"id": 0, "main": true, "summary": "故障处理段说明排查与恢复原则。"}]}'

        block = Block(text="检查接头。", heading="驱动器故障", location="第3-5页")
        with patch.object(analyzer, "chat", side_effect=fake_chat):
            analyzer._analyze_batch("维护手册.pdf", [(0, block)])

        self.assertIn("结构段：驱动器故障", captured["user"])
        self.assertNotIn("第3-5页", captured["user"])

    def test_work_record_summary_omits_generic_recorder_field(self):
        block = Block(
            text="- 时间：2026-11-11\n- 记录人：王同事\n"
                 "- 工作事项：更新差旅报销流程。\n- 结果：审批步骤已确认。",
            heading="流程更新",
        )
        with patch.object(analyzer, "settings", replace(settings, analysis_use_llm=False)):
            result = analyze([block], "财务工作记录.md")

        summary = result.sections[0].summary
        self.assertIn("2026-11-11", summary)
        self.assertIn("报销流程", summary)
        self.assertNotIn("王同事", summary)
        self.assertIn("记录人：王同事", result.sections[0].text, "原文保留供追溯")


if __name__ == "__main__":
    unittest.main()
