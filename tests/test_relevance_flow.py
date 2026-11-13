"""故障匹配与来源指标回归：不连接数据库、不加载模型、不调用搜索。"""
import unittest
from tests.network_test_support import online_api_test
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from app.core.answer import service
from app.domain.schemas import Chunk, ScoredChunk, SourceRef
from app.services import retrieval_service as rs


def audio_hit():
    text = ("# 音响没声音\n- 设备 / 部件：音响/小喇叭\n"
            "- 现象（遇到什么问题）：扫码没声音/电脑没声音\n"
            "- 处理过程（尝试了什么办法，是否更换备件）：需要将音响线留出一截插入音响中\n"
            "- 结果（是否解决）：解决\n- 原因：音响与电脑中间的音响线不能完全插入音响")
    return ScoredChunk(Chunk("audio", "audio-doc", "音响没声音.md", "md", "",
                             text, heading="音响没声音"), 2 / 61, 1.0,
                       semantic=0.95, bm25_used=True, semantic_used=True)


@online_api_test
class FaultAnswerFlowTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(service, "settings", replace(service.settings, llm_api_key="fake"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_audio_record_cannot_answer_computer_startup_or_web_failure(self):
        for question in ("电脑打不开", "电脑开不了机", "电脑网页打不开"):
            for fast in (True, False):
                with self.subTest(question=question, fast=fast), \
                        patch.object(service.retrieval, "search", return_value=[audio_hit()]), \
                        patch.object(service, "draft_solution") as model, \
                        patch.object(service, "llm_complete") as complete, \
                        patch.object(service, "web_search") as search:
                    result = service.answer(question, fast_mode=fast, web_search=False)
                self.assertFalse(result.solution_found)
                self.assertEqual(result.status, "not_found")
                self.assertNotIn("音响线", result.answer)
                self.assertNotIn("不在知识库覆盖", result.notice)
                self.assertEqual(result.sources, [])
                model.assert_not_called()
                complete.assert_not_called()
                search.assert_not_called()

    def test_same_record_still_answers_real_audio_fault(self):
        for question in ("音响没声音", "电脑没有声音"):
            with self.subTest(question=question), \
                    patch.object(service.retrieval, "search", return_value=[audio_hit()]):
                result = service.answer(question, fast_mode=True, web_search=False)
            self.assertTrue(result.solution_found)
            self.assertIn("音响线", result.answer)

    def test_history_rewrite_cannot_override_explicit_new_fault(self):
        with patch.object(service, "rewrite_query", return_value="音响没声音"), \
                patch.object(service.retrieval, "search", return_value=[audio_hit()]), \
                patch.object(service, "draft_solution") as draft:
            result = service.answer("电脑打不开", [{"role": "user", "content": "音响没声音"}], fast_mode=False)
        self.assertFalse(result.solution_found)
        self.assertEqual(result.sources, [])
        draft.assert_not_called()


@online_api_test
class SourceMetricTest(unittest.TestCase):
    def test_zero_negative_and_missing_metrics_are_distinct(self):
        hit = audio_hit()
        hit.semantic = 0.0
        result = service._sources([hit])[0].to_dict()
        self.assertEqual(result["rrf_score"], 0.032787)
        self.assertEqual(result["score"], 0.033)  # 兼容旧API，不冒充相似度。
        self.assertEqual(result["semantic_similarity"], 0)
        self.assertIsNone(result["rerank_score"])
        hit.semantic = -0.2
        hit.rerank_used = True
        self.assertEqual(service._sources([hit])[0].to_dict()["semantic_similarity"], -0.2)
        self.assertEqual(service._sources([hit])[0].to_dict()["rerank_score"], 0)
        hit.semantic = 0
        hit.semantic_used = False
        self.assertIsNone(service._sources([hit])[0].to_dict()["semantic_similarity"])

    def test_nonfinite_metrics_are_not_serialized_as_measurements(self):
        source = SourceRef("id", "doc", "", 0.033, "text", rrf_score=float("nan"),
                           keyword_coverage=True, semantic_similarity=float("inf"))
        result = source.to_dict()
        for key in ("rrf_score", "keyword_coverage", "semantic_similarity"):
            self.assertIsNone(result[key])

    def test_expanded_table_peers_do_not_inherit_anchor_scores(self):
        def table(cid):
            return Chunk(cid, "ledger", "资产台账", "xlsx", "", "", heading="设备",
                         table=[["设备", "IP"], [cid, "10.0.0.1"]])
        first, context, actual = table("first"), table("context"), table("actual")
        hits = [ScoredChunk(first, 0.033, 1.0, semantic=0.8, semantic_used=True),
                ScoredChunk(actual, 0.021, 0.5)]
        with patch.object(type(service.retrieval), "index", new_callable=PropertyMock,
                          return_value=SimpleNamespace(chunks=[first, context, actual])):
            expanded = service._expand_table_hits(hits)
        sources = {s.doc_id + s.snippet.splitlines()[-1]: s for s in service._sources(expanded)}
        self.assertEqual(len(sources), 3)
        values = [s.to_dict() for s in service._sources(expanded)]
        self.assertEqual(values[1]["scoring_method"], "context")
        for key in ("rrf_score", "keyword_coverage", "semantic_similarity", "rerank_score"):
            self.assertIsNone(values[1][key])
        self.assertEqual(values[2]["rrf_score"], 0.021)

    def test_equal_rrf_rank_does_not_mean_equal_cosine_similarity(self):
        chunk = Chunk("c1", "d1", "运维说明", "md", "", "接口故障排查")
        vectors = MagicMock()
        vectors.search.side_effect = [[("c1", 0.91)], [("c1", 0.71)]]
        with patch.object(rs, "VectorStore", return_value=vectors):
            retrieval = rs.RetrievalService()
        index = MagicMock()
        index.chunks = [chunk]
        index.search.return_value = [ScoredChunk(chunk, 5.0, 1.0, bm25_used=True)]
        with patch.object(type(retrieval), "index", new_callable=PropertyMock, return_value=index), \
                patch.object(rs, "get_embedder", return_value=MagicMock()):
            first = retrieval.search("接口故障一", use_rerank=False)[0]
            second = retrieval.search("接口故障二", use_rerank=False)[0]
        one, two = [service._sources([hit])[0].to_dict() for hit in (first, second)]
        self.assertEqual(one["rrf_score"], two["rrf_score"])
        self.assertNotEqual(one["semantic_similarity"], two["semantic_similarity"])
        self.assertIsNone(one["rerank_score"])

    def test_real_zero_rerank_is_distinguishable_from_skipped_rerank(self):
        vectors = MagicMock()
        with patch.object(rs, "VectorStore", return_value=vectors):
            retrieval = rs.RetrievalService()
        chunk = Chunk("c1", "d1", "记录", "md", "", "MES接口故障")
        index = MagicMock()
        index.search.return_value = [ScoredChunk(chunk, 5.0, 1.0, bm25_used=True)]
        reranker = MagicMock()
        reranker.score.return_value = [0.0]
        with patch.object(type(retrieval), "index", new_callable=PropertyMock, return_value=index), \
                patch.object(rs, "get_embedder", return_value=None), \
                patch.object(rs, "get_reranker", return_value=reranker):
            ranked = retrieval.search("MES接口故障")[0]
        self.assertEqual(service._sources([ranked])[0].to_dict()["rerank_score"], 0.0)


if __name__ == "__main__":
    unittest.main()
