import unittest

from app.core.answer.solutions import extract_solutions, render_solutions
from app.domain.schemas import Chunk, ScoredChunk


def hit(text="", table=None, cid="chunk", doc_id="doc", **changes):
    chunk = Chunk(id=cid, doc_id=doc_id, doc_name="运维记录.md", doc_type="md",
                  location="第 2 节", text=text, table=table, **changes)
    return ScoredChunk(chunk=chunk, score=1.0, coverage=1.0)


class ExtractSolutionsTest(unittest.TestCase):
    def test_supported_table_solution_headers(self):
        for label in ("处理方案", "解决方案", "处理方法", "解决方法", "处置措施", "维修措施",
                      "解决步骤", "操作步骤", "故障处理", "故障排查", "排查步骤", "修复步骤", "处置方法"):
            with self.subTest(label=label):
                source = hit(table=[["故障现象", label, "处理结果"],
                                    ["MES 报工超时", "检查接口日志，重启报工服务", "报工成功"]])
                items = extract_solutions([source], "MES 报工超时怎么办")
                self.assertEqual(len(items), 1)
                self.assertIs(items[0].hit, source)
                self.assertEqual(items[0].solution, "检查接口日志，重启报工服务")
                self.assertEqual(items[0].result, "报工成功")
                self.assertEqual(items[0].problem, "MES 报工超时")

    def test_text_colon_fields_and_original_steps(self):
        steps = "1. 检查交换机端口\n2. 将网线重新插紧\n3. 再次测试连通性"
        items = extract_solutions([hit("故障现象：网络不通\n处理方案：\n" + steps
                                       + "\n验证结果：连续测试 10 次正常\n记录人：张三")])
        self.assertEqual(items[0].solution, steps)
        self.assertEqual(items[0].result, "连续测试 10 次正常")
        self.assertEqual(items[0].problem, "网络不通")

    def test_inline_fields_separated_by_semicolons(self):
        items = extract_solutions([hit("问题描述：掉线；解决方案：重新插紧网线；处理结果：连接恢复")])
        self.assertEqual(items[0].solution, "重新插紧网线")
        self.assertEqual(items[0].result, "连接恢复")

    def test_original_pipe_command_is_preserved(self):
        text = "处理方案：\n1. 执行 ps aux | grep mes\n2. 核对进程状态\n处理结果：正常"
        item = extract_solutions([hit(text)])[0]
        self.assertEqual(item.solution, "1. 执行 ps aux | grep mes\n2. 核对进程状态")

    def test_bold_field_markers(self):
        for text in ("- **处理方案**：重启客户端\n**处理结果**：恢复正常",
                     "**处理方案：**重启客户端\n**处理结果：**恢复正常"):
            with self.subTest(text=text):
                items = extract_solutions([hit(text)])
                self.assertEqual(items[0].solution, "重启客户端")
                self.assertEqual(items[0].result, "恢复正常")

    def test_markdown_sections_preserve_substeps(self):
        text = ("# MES 故障记录\n## 故障现象\n报工失败\n## 处理方案\n"
                "1. 检查日志\n### 恢复操作\n2. 重启服务\n## 验证结果\n再次报工成功\n"
                "## 备注\n不要把备注当处理步骤")
        items = extract_solutions([hit(text)])
        self.assertEqual(items[0].solution, "1. 检查日志\n### 恢复操作\n2. 重启服务")
        self.assertEqual(items[0].result, "再次报工成功")

    def test_markdown_table_inside_source_text(self):
        text = ("# 维修台账\n\n| 故障现象 | 处理方案 | 验证结果 |\n| --- | --- | --- |\n"
                "| 相机掉线 | 1. 重新接线<br>2. 重启相机 | 连续采集正常 |")
        items = extract_solutions([hit(text)], "相机掉线怎么办")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].solution, "1. 重新接线\n2. 重启相机")
        self.assertEqual(items[0].result, "连续采集正常")

    def test_vertical_field_value_table(self):
        text = ("| 字段 | 内容 |\n| --- | --- |\n| 故障现象 | 客户端闪退 |\n"
                "| 解决方案 | 重新安装客户端 |\n| 处理结果 | 启动正常 |")
        item = extract_solutions([hit(text)])[0]
        self.assertEqual(item.problem, "客户端闪退")
        self.assertEqual(item.solution, "重新安装客户端")

    def test_placeholder_solutions_are_rejected(self):
        for value in ("", "未记录", "暂无", "无", "待处理", "已解决", "已处理", "恢复正常",
                      "暂无处理方案", "未记录具体方案", "具体处理步骤未记录", "N/A", "—",
                      "已解决，具体步骤未记录"):
            with self.subTest(value=value):
                self.assertEqual(extract_solutions([hit(f"处理方案：{value}\n处理结果：已解决")]), [])

    def test_status_with_substantive_action_remains_valid(self):
        items = extract_solutions([hit("处理方案：已解决，重新插紧网线")])
        self.assertEqual(items[0].solution, "已解决，重新插紧网线")

    def test_placeholder_and_vague_followup_are_not_a_solution(self):
        for value in ("暂无详细处理步骤，需进一步排查", "未记录具体方案，继续排查原因",
                      "处理方法未记录，建议后续跟进", "待进一步检查"):
            with self.subTest(value=value):
                self.assertEqual(extract_solutions([hit("处理方案：" + value)]), [])

    def test_real_actions_survive_missing_details_notice(self):
        for value in ("更换网线，具体型号未记录", "具体操作步骤未记录，重新插紧接口",
                      "进一步排查交换机端口是否掉线"):
            with self.subTest(value=value):
                self.assertEqual(extract_solutions([hit("处理方案：" + value)])[0].solution, value)

    def test_troubleshooting_section_preserves_concrete_steps(self):
        text = "## 故障排查\n1. 核对交换机端口指示灯\n2. 重新插紧网线\n## 处理结果\n通信恢复"
        item = extract_solutions([hit(text)])[0]
        self.assertEqual(item.solution, "1. 核对交换机端口指示灯\n2. 重新插紧网线")

    def test_problem_and_summary_are_not_used_to_invent_solution(self):
        self.assertEqual(extract_solutions([hit("故障现象：网线松脱\n处理结果：已解决",
                                               summary="重新插紧网线即可恢复")]), [])

    def test_multirow_table_only_selects_matching_device(self):
        rows = [["设备编号", "故障现象", "处理方案"],
                ["ZX-200", "网页打不开", "重启 200 客户端"],
                ["ZX-201", "网页打不开", "重装 201 客户端"]]
        items = extract_solutions([hit(table=rows)], "ZX-200 网页打不开怎么处理")
        self.assertEqual([item.solution for item in items], ["重启 200 客户端"])

    def test_multirow_table_uses_specific_problem_not_shared_system(self):
        rows = [["系统", "问题描述", "处理方案"], ["MES", "登录失败", "解锁账号"],
                ["MES", "报工超时", "重启接口服务"]]
        items = extract_solutions([hit(table=rows)], "MES 报工超时如何处理")
        self.assertEqual([item.solution for item in items], ["重启接口服务"])

    def test_generic_question_does_not_match_all_rows(self):
        rows = [["故障现象", "处理方案"], ["电机异响", "更换轴承"], ["相机掉线", "更换网线"]]
        self.assertEqual(extract_solutions([hit(table=rows)], "如何处理这个问题"), [])

    def test_unrelated_multirow_table_has_no_fallback(self):
        rows = [["故障现象", "处理方案"], ["电机异响", "更换轴承"], ["相机掉线", "更换网线"]]
        self.assertEqual(extract_solutions([hit(table=rows)], "MES 登录失败"), [])

    def test_single_row_can_use_existing_hit_relevance(self):
        rows = [["处理方案", "验证结果"], ["重启客户端", "启动正常"]]
        self.assertEqual(extract_solutions([hit(table=rows)], "客户端闪退")[0].solution, "重启客户端")

    def test_single_row_does_not_override_conflicting_identifier(self):
        rows = [["设备编号", "故障现象", "处理方案"], ["ZX-201", "网页打不开", "重装客户端"]]
        self.assertEqual(extract_solutions([hit(table=rows)], "ZX-200 网页打不开"), [])

    def test_shared_error_code_does_not_override_wrong_device(self):
        rows = [["设备编号", "故障现象", "处理方案"], ["ZX-201", "报 E-1024", "重装客户端"]]
        self.assertEqual(extract_solutions([hit(table=rows)], "ZX-200 报 E-1024"), [])

    def test_repeated_text_records_are_filtered_by_problem(self):
        text = ("问题描述：相机掉线\n处理方案：更换网线\n处理结果：恢复采集\n"
                "问题描述：电机异响\n处理方案：更换轴承\n处理结果：异响消失")
        items = extract_solutions([hit(text)], "电机异响怎么处理")
        self.assertEqual([item.solution for item in items], ["更换轴承"])

    def test_duplicate_chunks_keep_one_same_record(self):
        text = "处理方案：重新接线\n处理结果：通信恢复"
        self.assertEqual(len(extract_solutions([hit(text), hit(text, cid="overlap")])), 1)

    def test_parenthesized_template_fields_do_not_leak_result_or_cause_into_solution(self):
        text = ("- 设备/部件：音响/小喇叭\n"
                "- 现象（遇到什么问题）：扫码没声音/电脑没声音\n"
                "- 处理过程（尝试了什么办法，是否更换备件）：需要将音响线留出一截插入音响中\n"
                "- 结果（是否解决）：解决\n"
                "- 原因：音响与电脑中间音响线不能完全插入")
        item = extract_solutions([hit(text)], "电脑没有声音")[0]
        self.assertEqual(item.solution, "需要将音响线留出一截插入音响中")
        self.assertEqual(item.result, "解决")
        self.assertEqual(item.problem, "扫码没声音/电脑没声音")
        self.assertEqual(extract_solutions([hit(text)], "电脑打不开"), [])

    def test_same_device_keyword_does_not_override_single_record_fault_conflict(self):
        source = hit("故障现象：电脑没有声音\n处理方案：重新接音响线")
        self.assertEqual(extract_solutions([source], "电脑开不了机"), [])
        self.assertEqual(extract_solutions([source], "电脑网页打不开"), [])

    def test_single_row_different_device_same_symptom_has_no_fallback(self):
        rows = [["设备名称", "故障现象", "处理方案"], ["打印机", "无法开机", "更换打印机电源"]]
        self.assertEqual(extract_solutions([hit(table=rows)], "电脑开不了机"), [])


class RenderSolutionsTest(unittest.TestCase):
    def test_empty_evidence_has_no_answer(self):
        self.assertEqual(render_solutions([]), "")

    def test_solution_comes_first_with_verification_and_source(self):
        items = extract_solutions([hit("故障现象：网口不通\n处理方案：重新接线\n验证结果：通信恢复")])
        output = render_solutions(items)
        self.assertTrue(output.startswith("### 解决方案"))
        self.assertLess(output.index("重新接线"), output.index("通信恢复"))
        self.assertIn("来源：《运维记录.md》 · 第 2 节", output)
        self.assertIn("**原记录结果：** 通信恢复", output)
        self.assertNotIn("**验证：**", output)
        self.assertNotIn("故障现象", output)

    def test_vague_code_change_preserves_original_and_flags_missing_details(self):
        text = "联系技术同事，调整代码"
        output = render_solutions(extract_solutions([hit("处理方案：" + text)]))
        self.assertIn(text, output)
        self.assertIn("未注明具体修改位置、参数及代码内容", output)
        self.assertNotIn("重启", output)
        self.assertNotIn("第 10 行", output)

    def test_explicit_code_location_does_not_get_missing_location_notice(self):
        output = render_solutions(extract_solutions([hit("处理方案：修改 app.py 第12行代码，参数 timeout=30")]))
        self.assertNotIn("未注明", output)

    def test_multiple_solutions_are_separate(self):
        output = render_solutions(extract_solutions([hit("处理方案：重新接线"),
                                                     hit("处理方案：重启客户端", doc_id="other")]))
        self.assertIn("#### 方案 1", output)
        self.assertIn("#### 方案 2", output)


if __name__ == "__main__":
    unittest.main()
