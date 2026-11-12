import unittest

from app.domain.schemas import AnswerResponse, SourceRef


class AnswerResponseTest(unittest.TestCase):
    def test_existing_constructor_keeps_local_source_defaults(self):
        source = SourceRef("doc-1", "维修记录.md", "第2节", 0.98765, "重新插紧插头")
        result = AnswerResponse("ok", "处理记录", sources=[source]).to_dict()
        self.assertEqual(result["answer_origin"], "knowledge_base")
        self.assertFalse(result["solution_found"])
        self.assertEqual(result["sources"][0]["kind"], "knowledge_base")
        self.assertEqual(result["sources"][0]["url"], "")
        self.assertEqual(result["sources"][0]["score"], 0.988)
        self.assertEqual(result["sources"][0]["references"], [])

    def test_cited_document_is_serialized_under_its_parent_source(self):
        reference = SourceRef(
            "manual", "扫码枪手册.pdf", "", 0.0, "", scoring_method="reference",
            uploaded_at="2026-11-11 10:30:00",
        )
        source = SourceRef("record", "扫码枪处理记录.md", "章节一", 0.9, "扫描底座二维码",
                           references=[reference])
        other = SourceRef("other", "另一条记录.md", "章节二", 0.8, "重新接线")
        result = AnswerResponse("ok", "扫描底座二维码", sources=[source, other]).to_dict()
        self.assertEqual([item["doc_id"] for item in result["sources"]], ["record", "other"])
        nested = result["sources"][0]["references"]
        self.assertEqual([item["doc_id"] for item in nested], ["manual"])
        self.assertEqual(nested[0]["scoring_method"], "reference")
        self.assertEqual(nested[0]["uploaded_at"], "2026-11-11 10:30:00")
        self.assertEqual(nested[0]["references"], [])
        self.assertIsNone(nested[0]["rrf_score"])
        self.assertEqual(result["sources"][1]["references"], [])

    def test_web_reference_is_identifiable_in_serialized_response(self):
        source = SourceRef(
            "", "厂商技术说明", "", 0.9, "检查连接状态", kind="web",
            url="https://example.com/troubleshooting",
        )
        result = AnswerResponse(
            "ok", "联网参考", sources=[source], answer_origin="web",
            solution_found=True, notice="请结合现场核实",
        ).to_dict()
        self.assertEqual(result["answer_origin"], "web")
        self.assertTrue(result["solution_found"])
        self.assertEqual(result["sources"][0]["url"], source.url)
        self.assertEqual(result["sources"][0]["kind"], "web")
        self.assertEqual(result["notice"], "请结合现场核实")

    def test_unavailable_solution_has_explicit_origin(self):
        result = AnswerResponse(
            "not_found", "未找到方案", answer_origin="none", solution_found=False,
        ).to_dict()
        self.assertFalse(result["solution_found"])
        self.assertEqual(result["answer_origin"], "none")
        self.assertEqual(result["sources"], [])


if __name__ == "__main__":
    unittest.main()
