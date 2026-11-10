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
