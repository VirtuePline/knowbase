"""故障对象/现象一致性：仅使用内存分块，不访问数据库或模型。"""
import unittest

from app.core.answer.solutions import extract_solutions
from app.core.retrieval.relevance import fault_matches, filter_fault_hits
from app.domain.schemas import Chunk, ScoredChunk


_AUDIO_RECORD = (
    "- 设备/部件：音响/小喇叭\n"
    "- 现象（遇到什么问题）：扫码没声音/电脑没声音\n"
    "- 处理过程（尝试了什么办法，是否更换备件）：需要将音响线留出一截插入音响中\n"
    "- 结果（是否解决）：解决\n"
    "- 原因：音响与电脑中间音响线不能完全插入，否则没有声音。"
)


def hit(title="记录", text="", table=None, cid="record"):
    chunk = Chunk(id=cid, doc_id=cid, doc_name=title + ".md", doc_type="md",
                  location="记录章节", heading=title, text=text, table=table)
    return ScoredChunk(chunk=chunk, score=1.0, coverage=1.0, semantic=0.99, rerank=0.99)


class FaultIntentTest(unittest.TestCase):
    def test_same_computer_but_different_fault_is_rejected(self):
        for question in ("电脑打不开", "电脑开不了机", "电脑无法开机", "电脑网页打不开", "电脑无法登录"):
            with self.subTest(question=question):
                self.assertFalse(fault_matches(question, title="电脑没声音"))

    def test_speaker_cannot_open_is_not_an_audio_output_failure(self):
        for question in ("音响打不开", "音箱打不开"):
            with self.subTest(question=question):
                self.assertFalse(fault_matches(question, title="音响没声音", problem="电脑没有声音"))
                self.assertEqual(filter_fault_hits(question, [hit("音响没声音", _AUDIO_RECORD)]), [])
        self.assertTrue(fault_matches("音响没声音", title="音响没声音"))

    def test_list_solutions_wording_does_not_bypass_fault_conflicts(self):
        self.assertFalse(fault_matches("列出电脑开不了机的解决方案", title="电脑没有声音"))
        self.assertFalse(fault_matches("电脑开不了机有哪些办法", title="音响没有声音"))

    def test_audio_synonyms_and_related_computer_audio_are_kept(self):
        for question in ("音响没声音", "电脑没有声音", "音响没声", "电脑没声怎么处理"):
            with self.subTest(question=question):
                self.assertTrue(fault_matches(question, title="音响没声音", device="音响/小喇叭",
                                              problem="扫码没声音/电脑没声音"))

    def test_correct_boot_failure_is_kept(self):
        self.assertTrue(fault_matches("电脑打不开", title="工控机开不了机", problem="电脑无法启动"))
        self.assertTrue(fault_matches("电脑开不了机", title="开机故障", device="电脑", problem="黑屏"))

    def test_different_devices_cannot_share_same_failure_solution(self):
        self.assertFalse(fault_matches("电脑开不了机", title="打印机开不了机"))
        self.assertFalse(fault_matches("打印机没声音", title="音响没声音"))
        self.assertFalse(fault_matches("交换机不通电", title="电脑不通电"))

    def test_application_and_machine_startup_are_distinguished(self):
        self.assertTrue(fault_matches("MES系统无法启动", title="MES网页打不开"))
        self.assertFalse(fault_matches("电脑开不了机", title="MES客户端启动失败"))

    def test_network_access_can_be_related(self):
        self.assertTrue(fault_matches("电脑网页打不开", title="电脑断网", problem="无法联网"))
        self.assertFalse(fault_matches("电脑网页打不开", title="电脑没有声音"))

    def test_unknown_symptom_or_device_is_preserved_conservatively(self):
        self.assertTrue(fault_matches("电脑开不了机", title="设备异常处理记录"))
        self.assertTrue(fault_matches("电脑开不了机", title="电脑故障", problem="发生异常"))
        self.assertTrue(fault_matches("怎么处理这个问题", title="音响没声音"))

    def test_statistics_lists_and_field_lookups_are_untouched(self):
        for question in ("统计所有故障数量", "列出电脑故障记录", "电脑开不了机记录有多少条",
                         "音响的IP地址是多少", "ZX-200的型号", "10.10.0.231是哪个电脑"):
            with self.subTest(question=question):
                self.assertTrue(fault_matches(question, title="打印机没声音"))


class FilterFaultHitsTest(unittest.TestCase):
    def test_screenshot_audio_record_does_not_answer_startup_or_access(self):
        source = hit("音响没声音", _AUDIO_RECORD)
        for question in ("电脑打不开", "电脑开不了机", "电脑网页打不开"):
            with self.subTest(question=question):
                self.assertEqual(filter_fault_hits(question, [source]), [])
        for question in ("音响没声音", "电脑没有声音", "音响没声"):
            with self.subTest(question=question):
                self.assertEqual(filter_fault_hits(question, [source]), [source])

    def test_computer_only_in_action_and_cause_is_not_subject_evidence(self):
        source = hit("音响故障", "现象：音响没有声音\n处理方案：在电脑中调整音频输出\n"
                                 "原因：电脑开不了机时曾换过音响连接线")
        self.assertEqual(filter_fault_hits("电脑开不了机", [source]), [])

    def test_inline_action_fields_do_not_contaminate_problem(self):
        source = hit("音响故障", "现象：音响没有声音；处理过程：电脑开不了机时调整过接线；原因：线松脱")
        self.assertEqual(filter_fault_hits("电脑开不了机", [source]), [])

    def test_first_markdown_title_can_provide_fault_when_filename_is_generic(self):
        source = hit("记录", "# 音响没有声音\n\n处理方案：检查连接线。电脑重新开机后测试。")
        self.assertEqual(filter_fault_hits("电脑开不了机", [source]), [])

    def test_mixed_table_keeps_chunk_with_correct_row(self):
        rows = [["设备/部件", "故障现象", "处理方案"],
                ["音响", "电脑没有声音", "重新接音响线"],
                ["电脑", "电脑无法开机", "检查电源适配器"]]
        source = hit("音响没声音", table=rows)
        self.assertEqual(filter_fault_hits("电脑开不了机", [source]), [source])
        self.assertEqual([item.solution for item in extract_solutions([source], "电脑开不了机")],
                         ["检查电源适配器"])

    def test_all_conflicting_rows_drop_the_chunk(self):
        rows = [["设备名称", "故障现象", "处理方案"],
                ["音响", "电脑没有声音", "重新接音响线"],
                ["打印机", "无法打印", "更换耗材"]]
        self.assertEqual(filter_fault_hits("电脑开不了机", [hit(table=rows)]), [])

    def test_mixed_markdown_table_is_checked_row_by_row(self):
        text = ("| 设备 | 现象（遇到什么问题） | 处理方案 |\n| --- | --- | --- |\n"
                "| 音响 | 电脑没声音 | 重新接音响线 |\n| 电脑 | 开不了机 | 更换电源适配器 |")
        source = hit(text=text)
        self.assertEqual(filter_fault_hits("电脑开不了机", [source]), [source])
        self.assertEqual([item.solution for item in extract_solutions([source], "电脑开不了机")],
                         ["更换电源适配器"])

    def test_multiple_text_records_do_not_reject_a_correct_subject(self):
        text = ("设备：音响\n现象：没声音\n处理方案：检查音响线\n"
                "设备：电脑\n现象：开不了机\n处理方案：检查电源适配器")
        source = hit(text=text)
        self.assertEqual(filter_fault_hits("电脑开不了机", [source]), [source])
        self.assertEqual([item.solution for item in extract_solutions([source], "电脑开不了机")],
                         ["检查电源适配器"])

    def test_generic_maintenance_table_and_old_identifiers_are_not_dropped(self):
        rows = [["编号", "设备编号", "处理方案"], ["WX-1", "ZX-200", "检查电源"]]
        source = hit(table=rows)
        self.assertEqual(filter_fault_hits("ZX-200 E-1024", [source]), [source])
        self.assertEqual(filter_fault_hits("ZX-200的IP是什么", [source]), [source])

    def test_filter_preserves_order_scores_and_original_chunks(self):
        wrong = hit("音响没声音", _AUDIO_RECORD, cid="wrong")
        correct = hit("电脑无法开机", "故障现象：电脑开不了机\n处理方案：更换电源适配器", cid="right")
        other = hit("原因未明", cid="unknown")
        before = correct.chunk.to_dict()
        output = filter_fault_hits("电脑开不了机", [wrong, correct, other])
        self.assertEqual(output, [correct, other])
        self.assertIs(output[0], correct)
        self.assertEqual(output[0].chunk.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
