import unittest

from app.core.answer.context import render_excerpts, render_hits
from app.core.answer.service import _render_plan
from app.core.llm.client import SolutionDraft
from app.domain.schemas import Chunk, ScoredChunk
from app.domain.taxonomy import (
    FALLBACK_TEXT,
    classify,
    conflict_risk,
    is_management_request,
)


def hit(cid, doc_id, text, score, coverage=0.8, table=None):
    chunk = Chunk(id=cid, doc_id=doc_id, doc_name=f"{doc_id}.xlsx", doc_type="xlsx",
                  location="第2行", text=text, table=table)
    return ScoredChunk(chunk=chunk, score=score, coverage=coverage)


class TaxonomyTest(unittest.TestCase):
    def test_hardware_question(self):
        category, matched = classify("装配线输送电机异响，轴承位温度 82℃")
        self.assertEqual(category, "硬件故障排查")
        self.assertIn("硬件故障排查", matched)

    def test_mes_question_priority(self):
        category, _ = classify("MES 报工提交提示工单不存在")
        self.assertIn(category, {"MES报错排查", "报工异常", "工单流转异常"})

    def test_interface_question(self):
        category, _ = classify("MES 与 WMS 接口同步超时")
        self.assertEqual(category, "接口异常")

    def test_management_detection(self):
        for text in ("帮我删除 设备台账.xlsx", "查看上传列表", "把 MES运维记录.docx 删掉"):
            self.assertTrue(is_management_request(text), text)

    def test_mes_ops_not_mistaken_for_management(self):
        for text in ("MES 工单删除失败怎么办", "报工记录重复怎么删除", "MES 文件导入报错"):
            self.assertFalse(is_management_request(text), text)

    def test_fallback_text_is_fixed(self):
        self.assertTrue(FALLBACK_TEXT.startswith("【知识库未查询到匹配的记录】"))
        self.assertIn("核对主题、关键词", FALLBACK_TEXT)
        self.assertNotIn("运维", FALLBACK_TEXT)


class ConflictTest(unittest.TestCase):
    def test_same_document_no_conflict(self):
        self.assertFalse(conflict_risk([hit("a", "docA", "x", 10), hit("b", "docA", "y", 9.5)]))

    def test_different_documents_close_scores_conflict(self):
        self.assertTrue(conflict_risk([hit("a", "docA", "x", 10), hit("b", "docB", "y", 9.5)]))

    def test_different_documents_far_scores_no_conflict(self):
        self.assertFalse(conflict_risk([hit("a", "docA", "x", 10), hit("b", "docB", "y", 2)]))


class ContextTest(unittest.TestCase):
    def test_table_rendered_as_markdown(self):
        table = [["编号", "故障现象", "处理方案"], ["WX-1", "报 E-1024", "重新插紧插头"]]
        text = render_hits([hit("a", "docA", "", 5, table=table)])
        self.assertIn("| 编号 | 故障现象 | 处理方案 |", text)
        self.assertIn("| WX-1 | 报 E-1024 | 重新插紧插头 |", text)

    def test_excerpt_truncates_long_text(self):
        text = render_excerpts([hit("a", "docA", "故" * 5000, 5)])
        self.assertIn("……", text)

    def test_excerpt_lists_multiple_records(self):
        text = render_excerpts([hit("a", "docA", "记录一", 5), hit("b", "docB", "记录二", 4)])
        self.assertIn("记录 1", text)
        self.assertIn("记录 2", text)
        self.assertNotIn("docA.xlsx", text)
        self.assertNotIn("docB.xlsx", text)
        self.assertNotIn("第2行", text)


class RenderPlanTest(unittest.TestCase):
    def test_plans_keep_steps_and_verification_without_source_footer(self):
        plan = SolutionDraft(
            ["核对信号来源：PLC，并打开 https://device.example/status 检查状态。"],
            [1], "确认通信恢复。", "需核对设备型号。",
        )
        for web in (False, True):
            with self.subTest(web=web):
                answer = _render_plan(plan, web=web)
                self.assertIn(plan.steps[0], answer)
                self.assertIn(plan.verification, answer)
                self.assertIn(plan.limitations, answer)
                self.assertNotIn("**来源**", answer)
                self.assertNotIn("来源见下方", answer)


if __name__ == "__main__":
    unittest.main()
