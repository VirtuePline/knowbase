"""检索服务单例：BM25 关键词 + 向量语义的混合检索，启动时加载索引，重建后热替换。

向量检索：分块摘要由嵌入模型转成向量，存 MongoDB chunk_vectors 集合，
查询时计算余弦相似度；与 BM25 结果按 RRF（倒数排名融合）合并排序。
嵌入模型不可用（未下载、接口失败、配置关闭）时自动降级为纯 BM25，问答不中断。

查询端与管理端在同一进程时共用这个单例；分进程部署时，查询端在每次检索前比对索引文件的
修改时间，管理端重建索引后自动重新加载。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading

from app.config import settings
from app.core.ingest.pipeline import rebuild_index
from app.core.retrieval.bm25 import BM25Index
from app.core.retrieval.embeddings import EmbeddingError, get_embedder
from app.core.retrieval.reranker import RerankError, get_reranker
from app.core.retrieval.relevance import filter_fault_hits
from app.core.retrieval.vector_store import VectorStore
from app.domain.schemas import Chunk, ScoredChunk
from app.domain.taxonomy import KNOWLEDGE_CATEGORY, classify, vendor_hint, vendor_in_chunk
from app.services import access, database

# RRF 融合常数：排名越靠前贡献越大，两路都命中的分块得到叠加
_RRF_K = 60
# 召回宽度：BM25 与向量各取这么多候选再融合，保证错别字/口语化问法也能捞回台账分块
_RECALL = 10
# 向量库单次嵌入的分块批大小，避免一次性占用过多内存
_EMBED_BATCH = 32


def allowed_document_ids() -> set[str] | None:
    """权限来自当前数据库，不依赖可能尚未重建的共享检索索引。"""
    if access.is_internal():
        return None
    query = {"$and": [access.read_filter(), {"status": "ready"}]}
    return {str(row["_id"]) for row in database._docs().find(query, {"_id": 1})}


def authorized_hits(hits: list[ScoredChunk]) -> list[ScoredChunk]:
    allowed = allowed_document_ids()
    return hits if allowed is None else [hit for hit in hits if hit.chunk.doc_id in allowed]


def operations_mode(question: str = "") -> bool:
    """运维是企业知识库中的一个部门，其他部门不套用故障语义门禁。"""
    identity = access.current_identity() or {}
    if access.is_internal():
        return True
    if "operations" not in identity.get("department_ids", []):
        return False
    # 运维员工也会查询公共制度；只对其运维问题沿用专门的方案与故障规则。
    return (not question or bool(classify(question)[0] or vendor_hint(question))
            or bool(re.search(r"扫码枪|扫描枪|扫码器|读码器", question)))


def _embed_text(chunk: Chunk) -> str:
    # 与 BM25 保持一致的检索文本：章节标题 + 摘要（无摘要时退回原文）。
    # 常识库分块额外附带原文：台账单元格值（IP、MAC、设备号、使用人）是查询目标。
    text = f"{chunk.heading}\n{chunk.summary}" if chunk.summary else chunk.text
    if chunk.category == KNOWLEDGE_CATEGORY:
        text = f"{text}\n{chunk.text}"
    return text


def _embedding_hash(text: str) -> str:
    # 同名模型切换提供方 / API 地址后也要重新嵌入，避免混用不同向量空间。
    identity = [settings.embedding_provider, settings.embedding_model,
                settings.embedding_base_url if settings.embedding_provider == "api" else "", text]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


class RetrievalService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._index = BM25Index()
        self._vectors = VectorStore()
        self._loaded = False
        self._mtime = 0
        self._vector_error = ""

    @staticmethod
    def _file_mtime() -> int:
        try:
            return settings.index_file.stat().st_mtime_ns
        except OSError:
            return 0

    def load(self) -> None:
        if not self._loaded:
            with self._lock:
                if not self._loaded:
                    self._mtime = self._file_mtime()
                    self._index = BM25Index.load(settings.index_file)
                    self._loaded = True
                    self._sync_vectors(self._index)   # 旧索引首次启动时补齐向量，幂等

    def reload(self) -> None:
        with self._lock:
            self._mtime = self._file_mtime()
            self._index = BM25Index.load(settings.index_file)
            self._loaded = True
            self._sync_vectors(self._index)

    def rebuild(self) -> BM25Index:
        with self._lock:
            self._index = rebuild_index()
            self._mtime = self._file_mtime()
            self._loaded = True
            self._sync_vectors(self._index)
            return self._index

    def _sync_vectors(self, index: BM25Index) -> None:
        """嵌入新建或内容变化的分块、清理已删除分块；旧向量无哈希时自动补算。"""
        embedder = get_embedder()
        if embedder is None:
            self._vector_error = "向量检索已停用（embedding.enabled = false）"
            return
        try:
            model = settings.embedding_model
            existing = self._vectors.existing_hashes(model)
            texts = {c.id: _embed_text(c) for c in index.chunks}
            hashes = {cid: _embedding_hash(text) for cid, text in texts.items()}
            pending = [c for c in index.chunks if existing.get(c.id) != hashes[c.id]]
            for start in range(0, len(pending), _EMBED_BATCH):
                batch = pending[start:start + _EMBED_BATCH]
                vectors = embedder.embed([texts[c.id] for c in batch])
                if len(vectors) != len(batch):
                    raise EmbeddingError("嵌入结果数量与分块数量不一致")
                self._vectors.upsert(
                    [(c.id, c.doc_id, v) for c, v in zip(batch, vectors)], model,
                    content_hashes={c.id: hashes[c.id] for c in batch},
                )
            self._vectors.prune({c.id for c in index.chunks})
            self._vector_error = ""
        except EmbeddingError as exc:
            self._vector_error = str(exc)

    @property
    def index(self) -> BM25Index:
        self.load()
        if self._file_mtime() != self._mtime:   # 另一个进程重建了索引
            self.reload()
        return self._index

    def search(self, question: str, top_k: int = 6, vendor: str = "",
               use_rerank: bool = True, allowed_doc_ids: set[str] | None = None) -> list[ScoredChunk]:
        """混合检索：BM25 + 向量语义召回 → RRF 融合候选 → Cross-Encoder rerank 排序。

        召回宽度固定为 _RECALL 个分块（含错别字、口语化问法要靠向量召回捞回台账），
        但只对 RRF 排名前 rerank.candidates 个候选做 Cross-Encoder 精排：每条都要在
        CPU 上跑一遍，条数直接决定耗时（实测 10 条约 8s，6 条约 2.6s）。
        未进精排窗口的候选保留在结果尾部，覆盖率达标时仍可作为来源使用。
        use_rerank=False 时跳过精排（快速模式，检索 0.1s 内完成），按 RRF 排序返回。
        rerank 不可用（模型缺失 / 配置关闭）时退回 RRF 排序。
        vendor 非空时（问题点名了厂商，如「华智剔废乱剔」）该厂商的文档强制置顶，
        不受 rerank 打分影响。
        """
        authorized = allowed_document_ids()
        if authorized is not None:
            allowed_doc_ids = authorized if allowed_doc_ids is None else authorized & allowed_doc_ids
        if allowed_doc_ids is not None and not allowed_doc_ids:
            return []
        index = self.index
        candidate_count = max(top_k, _RECALL)
        scope = {"allowed_doc_ids": allowed_doc_ids} if allowed_doc_ids is not None else {}
        bm25_hits = index.search(question, top_k=candidate_count, **scope)
        vector_hits: list[ScoredChunk] = []
        embedder = get_embedder()
        if embedder is not None:
            try:
                query = embedder.query(question)
                by_id = {c.id: c for c in index.chunks}
                for chunk_id, similarity in self._vectors.search(
                    query, candidate_count, settings.embedding_model, **scope,
                ):
                    chunk = by_id.get(chunk_id)
                    if chunk is not None and (allowed_doc_ids is None or chunk.doc_id in allowed_doc_ids):
                        vector_hits.append(ScoredChunk(
                            chunk=chunk, score=similarity, coverage=0.0, semantic=similarity, semantic_used=True,
                        ))
            except EmbeddingError as exc:
                self._vector_error = str(exc)

        # RRF 融合：同一分块在两路的排名贡献叠加，保留各自的覆盖率与语义分
        fused: dict[str, ScoredChunk] = {}
        for hits in (bm25_hits, vector_hits):
            for rank, hit in enumerate(hits, start=1):
                entry = fused.get(hit.chunk.id)
                if entry is None:
                    entry = ScoredChunk(chunk=hit.chunk, score=0.0, coverage=hit.coverage, semantic=hit.semantic)
                    fused[hit.chunk.id] = entry
                entry.score += 1.0 / (_RRF_K + rank)
                entry.coverage = max(entry.coverage, hit.coverage)
                entry.bm25_used = entry.bm25_used or hit.bm25_used or hit.coverage > 0
                if hit.semantic_used:
                    entry.semantic = max(entry.semantic, hit.semantic) if entry.semantic_used else hit.semantic
                    entry.semantic_used = True
        candidates = sorted(fused.values(), key=lambda h: (h.score, h.semantic, h.coverage), reverse=True)
        # 在截断与精排之前排除明确的设备/故障冲突，给同故障的其他候选留下位置。
        if operations_mode(question):
            candidates = filter_fault_hits(question, candidates)

        if vendor:
            # 厂商文档置顶：问题描述与文档用词差异大时 rerank 会低估它们，
            # 这里强制保证厂商文档参与重排并排在最前
            matched = [h for h in candidates if vendor_in_chunk(h.chunk, vendor)]
            candidates = matched + [h for h in candidates if h not in matched]

        if not use_rerank:
            return candidates[:top_k]

        reranker = get_reranker()
        if reranker is not None and candidates:
            try:
                picks = candidates[:settings.rerank_candidates]
                if vendor:
                    # rerank 候选窗口可能把厂商文档挤在外面，补进来参与逐对打分
                    picks += [h for h in candidates
                              if vendor_in_chunk(h.chunk, vendor) and h not in picks][:4]
                # 送入 rerank 的文本按 320 字截断：cross-encoder 只看问题词是否出现在
                # 文本前半段，台账长文（上千字全文）截断后不影响相关性判断，
                # 实测 10 条候选的 rerank 从 8.0s 降到 4.4s
                texts = [_embed_text(h.chunk)[:320] for h in picks]
                scores = reranker.score(question, texts)
                for hit, score in zip(picks, scores):
                    hit.rerank = score
                    hit.rerank_used = True
                ranked = sorted(picks, key=lambda h: h.rerank, reverse=True)
                if vendor:
                    m = [h for h in ranked if vendor_in_chunk(h.chunk, vendor)]
                    ranked = m + [h for h in ranked if h not in m]
                return ranked[:top_k]
            except RerankError as exc:
                self._vector_error = str(exc)
        return candidates[:top_k]

    def stats(self) -> dict:
        index = self.index
        allowed = allowed_document_ids()
        chunks = index.chunks if allowed is None else [c for c in index.chunks if c.doc_id in allowed]
        if settings.llm_available:
            llm = settings.llm_model
        else:
            llm = "未配置（仅返回检索摘录）"
        embedder = get_embedder()
        if embedder is None:
            embedding = "已停用"
        else:
            scope = {"allowed_doc_ids": allowed} if allowed is not None else {}
            embedding = (f"{settings.embedding_model}（{self._vectors.count(settings.embedding_model, **scope)} 条向量，"
                         f"{self._vectors.backend()} 索引）")
            if self._vector_error:
                embedding += f"，最近异常：{self._vector_error}"
        return {
            "chunks": len(chunks),
            "documents": len({chunk.doc_id for chunk in chunks}),
            "llm": llm,
            "embedding": embedding,
            "rerank": "bge-reranker-base" if get_reranker() is not None else "已停用",
        }


retrieval = RetrievalService()
