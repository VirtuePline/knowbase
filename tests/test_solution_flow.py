"""方案优先与联网兜底回归，全部使用内存数据，不调用真实模型或搜索服务。"""
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from app.core.answer import context, service
from app.core.llm import client
from app.core.search.client import WebResult, WebSearchError
from app.domain.schemas import Chunk, ScoredChunk


def hit(text="箱签格式不正确", table=None):
    return ScoredChunk(Chunk("c1", "d1", "箱签记录.md", "md", "章节一", text,
                             table=table), score=1.0, coverage=1.0)


LEDGER_HEADER = ["序号", "品名", "IP", "使用人"]
LEDGER_HEADING = "主机、显示器台账"


def ledger_chunk(seq: int, rows: list[list[str]]) -> Chunk:
    return Chunk(
        id=f"lc{seq}", doc_id="d1", doc_name="信息部资产台账.xlsx", doc_type="xlsx",
        location=f"工作表「{LEDGER_HEADING}」 第{seq * 12 + 3}-{seq * 12 + 14}行",
        text="", heading=LEDGER_HEADING, table=[LEDGER_HEADER, *rows],
    )


def ledger_peers() -> list[Chunk]:
    """两个窗口块：前 10 台主机有 IP，后 10 台显示器 IP 空。"""
    with_ip = [[str(i), "主机", f"10.10.0.{i}", f"用户{i}"] for i in range(1, 11)]
    without_ip = [[str(i), "显示器", "", f"用户{i}"] for i in range(11, 21)]
    return [ledger_chunk(0, with_ip), ledger_chunk(1, without_ip)]


def ledger_anchor(peers: list[Chunk]) -> ScoredChunk:
    first = peers[0]
    return ScoredChunk(Chunk(
        "c0", first.doc_id, first.doc_name, first.doc_type,
        first.location, first.text, heading=first.heading, table=first.table,
    ), score=1.0, coverage=1.0)


class SolutionFlowTest(unittest.TestCase):
    def setUp(self):
        self.config = replace(service.settings, llm_api_key="", web_search_enabled=True,
                              web_search_api_key="search-test-key")
        config_patch = patch.object(service, "settings", self.config)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.search_patch = patch.object(service, "web_search", return_value=[])
        self.search = self.search_patch.start()
        self.addCleanup(self.search_patch.stop)
        # 运行时开关默认开启（不连 MongoDB）
        runtime_patch = patch.object(service, "web_search_runtime_enabled", return_value=True)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)
        # 默认走高质量路径（精排 + 模型整理）；快速模式另有专门用例
        fast_patch = patch.object(service, "fast_mode_enabled", return_value=False)
        fast_patch.start()
        self.addCleanup(fast_patch.stop)

    def test_table_solution_is_presented_first_without_model_or_search(self):
        record = hit(table=[["故障现象", "处理方案", "处理结果"],
                            ["箱签格式不正确", "联系技术同事，调整代码", "解决"]])
        with patch.object(service, "_top_hits", return_value=[record]), \
                patch.object(service, "draft_solution") as model:
            response = service.answer("箱签打印出来不对")
        self.assertTrue(response.solution_found)
        self.assertEqual(response.answer_origin, "knowledge_base")
        self.assertTrue(response.answer.startswith("### 解决方案"))
        self.assertIn("调整代码", response.answer)
        self.assertEqual(response.sources[0].doc_id, "d1")
        self.search.assert_not_called()
        model.assert_not_called()

    def test_problem_only_record_returns_excerpt_instead_of_search(self):
        """命中但没有处理方案（如台账字段查询）：直接给原文摘录，不转联网。"""
        with patch.object(service, "_top_hits", return_value=[hit()]):
            response = service.answer("箱签打印出来不对")
        self.search.assert_not_called()
        self.assertFalse(response.solution_found)
        self.assertEqual(response.status, "not_found")
        self.assertEqual(response.answer_origin, "knowledge_base")
        self.assertIn("](doc://", response.answer)   # 记录标题已转为可点击引用
        self.assertIn("箱签格式不正确", response.answer)
        self.assertEqual(response.sources[0].doc_id, "d1")

    def test_fast_mode_skips_model_and_returns_excerpt(self):
        """快速模式：命中即回原文，不调用模型整理。"""
        with patch.object(service, "fast_mode_enabled", return_value=True), \
                patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[hit()]), \
                patch.object(service, "draft_solution") as model:
            response = service.answer("箱签打印出来不对")
        model.assert_not_called()
        self.assertFalse(response.llm_used)
        self.assertEqual(response.answer_origin, "knowledge_base")
        self.assertIn("快速模式", response.notice)
        self.assertIn("](doc://", response.answer)   # 记录标题已转为可点击引用

    def test_fast_mode_skips_rerank_in_retrieval(self):
        """快速模式检索不跑 Cross-Encoder 精排。"""
        with patch.object(service, "fast_mode_enabled", return_value=True), \
                patch.object(service.retrieval, "search", return_value=[]) as search:
            service.answer("箱签打印出来不对")
        self.assertFalse(search.call_args.kwargs.get("use_rerank", True))

    def test_missing_search_key_does_not_make_network_call(self):
        """缺少搜索密钥：即使要求联网也不发起网络调用，回退知识库摘录。"""
        with patch.object(service, "settings", replace(self.config, web_search_api_key="")), \
                patch.object(service, "_top_hits", return_value=[hit()]):
            response = service.answer("MES报错怎么办", llm_polish=True, web_search=True)
        self.search.assert_not_called()
        self.assertFalse(response.solution_found)
        self.assertIn("联网搜索未配置", response.notice)

    def test_runtime_switch_off_blocks_search(self):
        """全局联网开关关闭：知识库零命中时不发起联网搜索。"""
        with patch.object(service, "web_search_runtime_enabled", return_value=False), \
                patch.object(service, "_top_hits", return_value=[]):
            response = service.answer("MES报错怎么办")
        self.search.assert_not_called()
        self.assertFalse(response.solution_found)
        self.assertIn("未做联网搜索", response.notice)

    def test_request_web_search_overrides_runtime_off(self):
        """本次请求显式开启联网：全局开关关闭也照样联网；搜索失败回退知识库原文。"""
        with patch.object(service, "web_search_runtime_enabled", return_value=False), \
                patch.object(service, "_top_hits", return_value=[]), \
                patch.object(service, "web_search", side_effect=service.WebSearchError("down")):
            response = service.answer("MES报错怎么办", web_search=True)
        self.assertEqual(response.answer_origin, "none")

    def test_request_web_search_falls_back_to_kb_excerpts(self):
        """本次开启联网且知识库有相关记录但无方案：联网失败时回退原文展示。"""
        with patch.object(service, "web_search_runtime_enabled", return_value=False), \
                patch.object(service, "_top_hits", return_value=[hit()]), \
                patch.object(service, "web_search", return_value=[]):
            response = service.answer("MES报错怎么办", llm_polish=True, web_search=True)
        self.assertEqual(response.answer_origin, "knowledge_base")
        self.assertIn("联网搜索也未找到可用参考", response.notice)

    def test_web_solution_has_explicit_external_source(self):
        """知识库有相关记录但提取不到方案：联网能出可执行方案时标注外部来源。"""
        self.search.return_value = [WebResult("厂商手册", "https://example.com/manual",
                                             "打印偏移时重新校准纸张。", 0.9)]
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[hit()]), \
                patch.object(service, "llm_complete", return_value="NO_ANSWER"), \
                patch.object(service, "draft_solution",
                             side_effect=[None, client.SolutionDraft(
                                 ["重新校准纸张。"], [1], "打印测试标签确认位置。")]) as model:
            response = service.answer("箱签打印偏移", llm_polish=True, web_search=True)
        self.assertTrue(response.solution_found)
        self.assertEqual(response.answer_origin, "web")
        self.assertEqual(response.sources[0].kind, "web")
        self.assertEqual(response.sources[0].url, "https://example.com/manual")
        self.assertIn("尚未", response.notice)
        self.assertIsNone(model.call_args.args[2])

    def test_search_snippets_are_not_mislabelled_as_solution_without_model(self):
        """联网只搜到论坛提问等无方案内容：不把网页片段伪装成解决方案。"""
        self.search.return_value = [WebResult("论坛问题", "https://example.com/question", "我的打印机坏了")]
        with patch.object(service, "_top_hits", return_value=[hit()]):
            response = service.answer("贴标机不出标签", llm_polish=True, web_search=True)
        self.assertFalse(response.solution_found)
        self.assertEqual(response.answer_origin, "web")
        self.assertEqual(response.status, "not_found")
        self.assertIn("未能从中确认可执行方案", response.answer)

    def test_out_of_scope_question_never_searches(self):
        """既无设备/系统关键词、也非厂商问题：不联网，直接说明超出范围。"""
        with patch.object(service, "_top_hits", return_value=[]):
            response = service.answer("火星土壤的成分检测标准是什么")
        self.search.assert_not_called()
        self.assertEqual(response.status, "not_found")
        self.assertEqual(response.answer_origin, "none")
        self.assertIn("不在知识库覆盖", response.notice)
        self.assertIn("知识库未查询到匹配的记录", response.answer)

    def test_in_scope_question_without_hits_skips_search(self):
        """知识库领域内、但一条都没命中：不再联网，直接说明无相关记录。"""
        with patch.object(service, "_top_hits", return_value=[]):
            response = service.answer("MES报错提示登录失败")
        self.search.assert_not_called()
        self.assertFalse(response.solution_found)
        self.assertEqual(response.status, "not_found")
        self.assertEqual(response.answer_origin, "none")
        self.assertIn("知识库中没有与当前问题相关的记录", response.answer)
        self.assertIn("未做联网搜索", response.notice)

    def test_search_failure_is_nonfatal(self):
        """联网搜索失败：回退展示知识库相关记录原文，不把失败当回答。"""
        self.search.side_effect = WebSearchError("服务暂不可用")
        with patch.object(service, "_top_hits", return_value=[hit()]):
            response = service.answer("MES接口超时", llm_polish=True, web_search=True)
        self.assertEqual(response.status, "not_found")
        self.assertIn("联网搜索暂时失败", response.notice)

    def test_unstructured_manual_can_be_solved_by_model_without_search(self):
        record = hit("打开打印首选项，将纸张大小与标签规格保持一致。")
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[record]), \
                patch.object(service, "draft_solution", return_value=client.SolutionDraft(
                    ["打开打印首选项，将纸张大小与标签规格保持一致。"], [1])):
            response = service.answer("箱签纸张大小不对", llm_polish=True)
        self.assertTrue(response.solution_found)
        self.assertTrue(response.llm_used)
        self.search.assert_not_called()

    def test_stats_question_uses_full_sheet_and_marks_unregistered(self):
        """统计类问题：合并同工作表全部窗口行，模型基于完整台账逐行核对作答。"""
        peers = ledger_peers()
        fake_retrieval = SimpleNamespace(index=SimpleNamespace(chunks=peers))
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[ledger_anchor(peers)]), \
                patch.object(service, "retrieval", fake_retrieval), \
                patch.object(service, "draft_solution") as model, \
                patch.object(service, "llm_complete",
                             return_value="台账 20 行记录中，2 号显示器与 3 号显示器的 IP 未登记。") as qa:
            response = service.answer("有几台设备没有IP", llm_polish=True)
        model.assert_not_called()
        self.assertEqual(response.status, "ok")
        self.assertTrue(response.llm_used)
        self.assertFalse(response.solution_found)
        self.assertEqual(response.answer_origin, "knowledge_base")
        self.assertIn("IP 未登记", response.answer)
        self.assertIn("完整台账", response.notice)
        self.assertIn("不代表设备实际状态", response.notice)
        question_arg, context_arg = qa.call_args[0][:2]
        self.assertIn("未登记", question_arg)           # 措辞指导已随问题送入
        self.assertTrue(question_arg.startswith("有几台设备没有IP"))  # 原始问题保持原样
        self.assertIn("完整台账", context_arg)
        self.assertIn("10.10.0.10", context_arg)        # 第一个窗口的最后一行
        self.assertIn("| 20 | 显示器 |  | 用户20 |", context_arg)  # 第二个窗口的数据行
        self.assertEqual(len(response.sources), 2)      # 两个窗口块都列为来源
        self.search.assert_not_called()

    def test_stats_question_falls_back_when_model_cannot_answer(self):
        """模型连续 NO_ANSWER：落回摘录，且摘录来源为完整工作表的全部窗口块。"""
        peers = ledger_peers()
        fake_retrieval = SimpleNamespace(index=SimpleNamespace(chunks=peers))
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[ledger_anchor(peers)]), \
                patch.object(service, "retrieval", fake_retrieval), \
                patch.object(service, "draft_solution", return_value=None), \
                patch.object(service, "llm_complete", return_value="NO_ANSWER") as qa:
            response = service.answer("有几台设备没有IP", llm_polish=True)
        self.assertEqual(qa.call_count, 3)      # 统计重试一次后仍 NO_ANSWER，问答兜底再试一次
        self.assertEqual(response.status, "not_found")
        self.assertFalse(response.llm_used)
        self.assertEqual(response.answer_origin, "knowledge_base")
        self.assertIn("](doc://", response.answer)   # 记录标题已转为可点击引用
        self.assertEqual(len(response.sources), 2)               # 摘录来源为两个窗口块
        self.search.assert_not_called()

    def test_ledger_stats_retries_once_after_no_answer(self):
        """统计调用首次 NO_ANSWER（模型波动）时重试一次，第二次成功即返回统计结果。"""
        peers = ledger_peers()
        fake_retrieval = SimpleNamespace(index=SimpleNamespace(chunks=peers))
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[ledger_anchor(peers)]), \
                patch.object(service, "retrieval", fake_retrieval), \
                patch.object(service, "llm_complete",
                             side_effect=["NO_ANSWER", "台账 20 行记录中，10 台显示器未登记 IP。"]):
            response = service.answer("有几台设备没有IP", llm_polish=True)
        self.assertEqual(response.status, "ok")
        self.assertIn("完整台账", response.notice)
        self.assertIn("未登记 IP", response.answer)

    def test_expand_table_hits_merges_all_windows_in_order(self):
        """扩展：命中的表格块展开为完整工作表全部窗口，顺序与文档一致且去重。"""
        peers = ledger_peers()
        anchor = ledger_anchor(peers)          # 只命中第一个窗口
        noise = hit()                          # 非表格记录保持原样
        fake_retrieval = SimpleNamespace(index=SimpleNamespace(chunks=peers))
        with patch.object(service, "retrieval", fake_retrieval):
            expanded = service._expand_table_hits([noise, anchor])
        self.assertEqual([h.chunk.id for h in expanded], ["c1", "lc0", "lc1"])
        self.assertEqual(expanded[1].chunk.table[0], LEDGER_HEADER)

    def test_non_stats_question_skips_ledger_stats(self):
        """故障类问题不走统计流程，保持方案提取路径。"""
        peers = ledger_peers()
        fake_retrieval = SimpleNamespace(index=SimpleNamespace(chunks=peers))
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[ledger_anchor(peers)]), \
                patch.object(service, "retrieval", fake_retrieval), \
                patch.object(service, "draft_solution",
                             return_value=client.SolutionDraft(["重启打印机"], [1])) as model:
            response = service.answer("资产台账里是什么内容", llm_polish=True)
        model.assert_called_once()
        self.assertTrue(response.solution_found)
        """模型认为没有方案且问答模式也无答案：展示知识库原文，不转联网。"""
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[hit()]), \
                patch.object(service, "draft_solution", return_value=None), \
                patch.object(service, "llm_complete", return_value="NO_ANSWER"):
            response = service.answer("箱签打印出来不对")
        self.search.assert_not_called()
        self.assertFalse(response.solution_found)
        self.assertEqual(response.answer_origin, "knowledge_base")
        self.assertIn("](doc://", response.answer)   # 记录标题已转为可点击引用

    def test_llm_qa_answers_ledger_questions_without_solution_steps(self):
        """台账统计类问题没有处理步骤可提取：问答模式基于记录直接回答。"""
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[hit("主机台账第2行，IP列为空")]), \
                patch.object(service, "draft_solution", return_value=None), \
                patch.object(service, "llm_complete", return_value="记录片段中有 1 台设备 IP 为空。") as qa:
            response = service.answer("有几台设备没有IP", llm_polish=True)
        qa.assert_called_once()
        self.assertEqual(response.status, "ok")
        self.assertTrue(response.llm_used)
        self.assertFalse(response.solution_found)
        self.assertEqual(response.answer_origin, "knowledge_base")
        self.assertIn("1 台设备 IP 为空", response.answer)
        self.assertIn("模型基于知识库相关记录整理", response.notice)
        self.assertEqual(response.sources[0].doc_id, "d1")
        self.search.assert_not_called()

    def test_default_mode_skips_model_without_llm_polish(self):
        """默认模式（未开大模型整理）：精排后直接摘录/规则提取，不调用模型。"""
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[hit()]), \
                patch.object(service, "draft_solution") as model, \
                patch.object(service, "llm_complete") as qa:
            response = service.answer("箱签打印出来不对")
        model.assert_not_called()
        qa.assert_not_called()
        self.assertFalse(response.llm_used)
        self.assertEqual(response.answer_origin, "knowledge_base")

    def test_llm_qa_failure_still_returns_excerpt(self):
        """问答模式调用失败时维持原文摘录回退。"""
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[hit()]), \
                patch.object(service, "draft_solution", return_value=None), \
                patch.object(service, "llm_complete", side_effect=service.LLMError("down")):
            response = service.answer("箱签打印出来不对", llm_polish=True)
        self.assertFalse(response.llm_used)
        self.assertEqual(response.status, "not_found")
        self.assertEqual(response.answer_origin, "knowledge_base")
        self.assertIn("](doc://", response.answer)   # 记录标题已转为可点击引用

    def test_invalid_source_number_cannot_be_presented_as_solution(self):
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", return_value=[hit()]), \
                patch.object(service, "draft_solution", return_value=client.SolutionDraft(["凭空方案"], [9])), \
                patch.object(service, "llm_complete", return_value="NO_ANSWER"):
            response = service.answer("箱签打印出来不对", llm_polish=True)
        self.assertFalse(response.solution_found)
        self.assertNotIn("凭空方案", response.answer)
        self.search.assert_not_called()
        self.assertIn("](doc://", response.answer)   # 记录标题已转为可点击引用

    def test_stats_question_recalls_table_below_threshold(self):
        """统计类问题的关键词不在台账正文里，低于门槛时放行分数最高的表格块兜底。"""
        low = ScoredChunk(Chunk("lc0", "d1", "信息部资产台账.xlsx", "xlsx",
                                "工作表「主机、显示器台账」 第3-14行", "",
                                heading=LEDGER_HEADING, table=[LEDGER_HEADER, ["1", "主机", "", "用户1"]]),
                          score=0.03, coverage=0.21, semantic=0.49, rerank=0.06)
        noise = ScoredChunk(Chunk("n1", "d2", "说明.md", "md", "章节", "纯文本内容"),
                            score=0.02, coverage=0.0, semantic=0.5, rerank=0.0)
        fake_retrieval = SimpleNamespace(search=lambda *a, **k: [noise, low])
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "retrieval", fake_retrieval):
            # 统计类问题：放行表格块，交完整台账给模型判断
            hits = service._top_hits("有几台设备没有IP")
            self.assertEqual([h.chunk.id for h in hits], ["lc0"])
            # 点查/故障类问题：维持原有门槛，不放行
            self.assertEqual(service._top_hits("李明远的IP是多少"), [])

    def test_answer_passes_rejected_docs_to_top_hits(self):
        """「重新回答」时否决的来源文档透传给检索门槛。"""
        with patch.object(service, "_top_hits", return_value=[]) as top:
            service.answer("网页打不开", rejected_doc_ids=["d2"])
            self.assertEqual(top.call_args.kwargs.get("reject_docs"), ["d2"])

    def test_rejected_sources_excluded_and_penalized(self):
        """惩罚参数：显式拒绝的来源直接剔除；历史否决按次数衰减，压线候选掉出门槛。"""

        def make_hits():
            # 每次返回新对象：惩罚会原地衰减分数，避免用例之间互相污染
            return [
                ScoredChunk(Chunk("m1", "d2", "补号的操作步骤.docx", "docx", "章节",
                                  "补号操作 制造资源中选择印刷文件生成后导出需求文件"),
                            score=0.015, coverage=0.0, semantic=0.45, rerank=0.13),
                ScoredChunk(Chunk("s1", "d3", "箱签记录.md", "md", "章节",
                                  "设备 IP：10.10.0.117 箱签打印不出 处理过程：检查打印机地址"),
                            score=0.09, coverage=1.0, semantic=0.7, rerank=0.9),
            ]

        fake_retrieval = SimpleNamespace(search=lambda *a, **k: make_hits())
        with patch.object(service, "retrieval", fake_retrieval), \
                patch.object(service, "answer_penalty_counts", return_value={}) as counts:
            # 无惩罚记录：两个候选都按原门槛放行
            hits = service._top_hits("网页打不开")
            self.assertEqual([h.chunk.id for h in hits], ["m1", "s1"])
            self.assertEqual(counts.call_count, 1)
            # 「重新回答」显式拒绝 d2：直接剔除，不做衰减
            hits = service._top_hits("网页打不开", reject_docs=["d2"])
            self.assertEqual([h.chunk.id for h in hits], ["s1"])
        with patch.object(service, "retrieval", fake_retrieval), \
                patch.object(service, "answer_penalty_counts", return_value={"d2": 1}):
            # 历史否决一次：压线候选（m1）rerank 0.13-0.2、语义 0.45-0.05 掉出门槛
            self.assertEqual([h.chunk.id for h in service._top_hits("网页打不开")], ["s1"])
        with patch.object(service, "retrieval", fake_retrieval), \
                patch.object(service, "answer_penalty_counts", return_value={"d3": 1}):
            # 惩罚只轻微影响强相关记录：rerank 0.9-0.2、语义 0.7-0.05 仍远高于门槛
            hits = service._top_hits("网页打不开")
            self.assertEqual([h.chunk.id for h in hits], ["m1", "s1"])

    def test_context_aware_rewrite_feeds_retrieval_only(self):
        """关闭快速模式且有历史：改写后的查询参与检索，回答仍用工程师原话+历史。"""
        peers = ledger_peers()
        fake_retrieval = SimpleNamespace(index=SimpleNamespace(chunks=peers))
        captured = {}

        def fake_top_hits(question, vendor="", fast=False, reject_docs=None):
            captured["query"] = question
            return [ledger_anchor(peers)]

        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", fake_top_hits), \
                patch.object(service, "retrieval", fake_retrieval), \
                patch.object(service, "rewrite_query", return_value="那几台没有IP的设备的MAC地址") as rw, \
                patch.object(service, "draft_solution", return_value=None), \
                patch.object(service, "llm_complete", return_value="NO_ANSWER"):
            response = service.answer("那几台的MAC呢", llm_polish=True, history=[
                {"role": "user", "content": "有几台设备没有IP"},
                {"role": "assistant", "content": "23 台显示器未登记 IP。"},
            ])
        rw.assert_called_once()
        self.assertEqual(captured["query"], "那几台没有IP的设备的MAC地址")  # 检索用改写查询
        self.assertEqual(response.status, "not_found")                      # 摘录回退
        self.assertIn("已结合上一轮对话补全检索关键词", response.notice)

    def test_rewrite_skipped_in_fast_mode_and_without_history(self):
        """快速模式或无历史：不调用改写，检索用原问题。"""
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "rewrite_query") as rw, \
                patch.object(service, "_top_hits", return_value=[]) as top:
            service.answer("打印偏移", fast_mode=True, history=[
                {"role": "user", "content": "之前问过别的"}])
            service.answer("打印偏移", fast_mode=False, history=[])
        rw.assert_not_called()
        self.assertEqual(top.call_args_list[0].args[0], "打印偏移")
        self.assertEqual(top.call_args_list[1].args[0], "打印偏移")

    def test_rewrite_failure_falls_back_to_original_question(self):
        """改写调用失败：回退原问题检索，问答流程不中断。"""
        peers = ledger_peers()
        fake_retrieval = SimpleNamespace(index=SimpleNamespace(chunks=peers))
        captured = {}

        def fake_top_hits(question, vendor="", fast=False, reject_docs=None):
            captured["query"] = question
            return [ledger_anchor(peers)]

        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "_top_hits", fake_top_hits), \
                patch.object(service, "retrieval", fake_retrieval), \
                patch.object(service, "rewrite_query", side_effect=client.LLMError("超时")), \
                patch.object(service, "draft_solution", return_value=None), \
                patch.object(service, "llm_complete", return_value="NO_ANSWER"):
            response = service.answer("那几台的MAC呢", llm_polish=True, history=[
                {"role": "user", "content": "有几台设备没有IP"}])
        self.assertEqual(captured["query"], "那几台的MAC呢")
        self.assertNotIn("已结合上一轮对话", response.notice)

    def test_stats_question_promotes_table_hit_above_text_records(self):
        """统计类问题：故障记录分数更高时，台账表格块仍被置顶交给统计流程。"""
        ledger = ScoredChunk(Chunk("lc0", "d1", "信息部资产台账.xlsx", "xlsx",
                                   "工作表「主机、显示器台账」 第3-14行", "",
                                   heading=LEDGER_HEADING,
                                   table=[LEDGER_HEADER, ["1", "主机", "", "用户1"]]),
                             score=0.03, coverage=0.21, semantic=0.49, rerank=0.06)
        record = ScoredChunk(Chunk("r1", "d2", "箱签打印不出.md", "md", "章节",
                                   "设备 IP：10.10.0.117 一线打印机不出箱签 处理过程：检查打印机地址"),
                             score=0.09, coverage=0.5, semantic=0.62, rerank=0.2)
        fake_retrieval = SimpleNamespace(search=lambda *a, **k: [record, ledger])
        with patch.object(service, "settings", replace(self.config, llm_api_key="llm-test")), \
                patch.object(service, "retrieval", fake_retrieval):
            hits = service._top_hits("哪些设备有没有设置IP")
            self.assertEqual(hits[0].chunk.id, "lc0")            # 表格块置顶
            self.assertEqual([h.chunk.id for h in hits][1:], ["r1"])
            # 非统计问题维持原有相关性排序，不受置顶影响
            hits = service._top_hits("箱签打印出来不对")
            self.assertEqual(hits[0].chunk.id, "r1")

    def test_table_excerpt_focuses_on_matching_rows(self):
        """大表摘录只保留表头与命中行；窗口内的小表整表保留，避免误筛掉数据行。"""
        rows = [["使用人", "IP", "标签"]]
        rows += [[f"同事{i}", f"10.10.0.{i}", f"主机-{i:03d}"] for i in range(1, 25)]
        rows[12] = ["李明远", "192.168.33.7", "主机-032"]
        record = hit(text="", table=rows)
        out = context.render_excerpts([record], question="李明远的IP是多少")
        self.assertIn("192.168.33.7", out)
        self.assertNotIn("10.10.0.5", out)
        self.assertIn("使用人", out)

        small = [["使用人", "IP", "标签"],
                 ["李明远", "192.168.33.7", "主机-032"],
                 ["张三", "10.10.0.9", "主机-099"]]
        out_small = context.render_excerpts([hit(text="", table=small)], question="IP字段在哪一列")
        self.assertIn("192.168.33.7", out_small)
        self.assertIn("10.10.0.9", out_small)

    def test_empty_and_management_requests_do_not_search(self):
        for question in ("", "帮我删除 设备台账.xlsx"):
            self.assertEqual(service.answer(question).status, "guide")
        self.search.assert_not_called()


class SolutionProtocolTest(unittest.TestCase):
    def test_rewrite_query_uses_reply_and_falls_back_on_bad_output(self):
        from app.core.llm.client import rewrite_query
        history = [{"role": "user", "content": "有几台设备没有IP"},
                   {"role": "assistant", "content": "23 台显示器未登记 IP。"}]
        with patch.object(client, "_chat_openai",
                          return_value="那几台没有IP的设备的MAC地址是多少") as chat:
            self.assertEqual(rewrite_query("那几台的MAC呢", history), "那几台没有IP的设备的MAC地址是多少")
            self.assertEqual(chat.call_args[0][0][-1]["role"], "user")   # 历史轮次在前
        # 空回复 / 超长输出 / 带引号：回退原问题
        with patch.object(client, "_chat_openai", return_value=""):
            self.assertEqual(rewrite_query("那几台的MAC呢", history), "那几台的MAC呢")
        with patch.object(client, "_chat_openai", return_value="很长的" * 60):
            self.assertEqual(rewrite_query("那几台的MAC呢", history), "那几台的MAC呢")
        with patch.object(client, "_chat_openai", return_value='"改写结果"'):
            self.assertEqual(rewrite_query("那几台的MAC呢", history), "改写结果")

    def draft(self, data):
        reply = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        with patch.object(client, "_chat_openai", return_value=reply):
            return client.draft_solution("打印偏移", "【记录 1】校准纸张")

    def test_valid_protocol(self):
        plan = self.draft({"solution_found": True, "steps": ["校准纸张"], "source_records": [1]})
        self.assertEqual(plan.steps, ["校准纸张"])

    def test_problem_description_and_no_solution_protocol_are_rejected(self):
        for data in ("打印格式有问题", "NO_ANSWER", {"solution_found": False},
                     {"solution_found": True, "steps": [], "source_records": [1]},
                     {"solution_found": True, "steps": ["未记录"], "source_records": [1]},
                     {"solution_found": True, "steps": ["校准"], "source_records": [True]},
                     {"solution_found": True, "steps": ["校准"], "source_records": []}):
            with self.subTest(data=data):
                self.assertIsNone(self.draft(data))


if __name__ == "__main__":
    unittest.main()
