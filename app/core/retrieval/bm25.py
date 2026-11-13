"""BM25 索引与持久化。

磁盘上只保存分块本身，词频统计在加载时重新计算，避免索引格式与分词规则不一致。
"""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

from app.core.retrieval.tokenizer import query_terms, tokenize
from app.domain.schemas import Chunk, ScoredChunk

INDEX_VERSION = 1


def _chunk_tokens(chunk: Chunk) -> list[str]:
    # 章节标题参与检索：Markdown 记录的故障名称通常只写在标题里。
    # 经过文档分析的分块只按摘要检索，原文仅作为元数据；旧分块没有摘要时退回原文。
    # 文档库中的台账单元格值（IP、MAC、设备号、使用人、配置）就是查询目标，
    # 原文全文参与检索，否则「74是哪个电脑」这类按值提问永远检索不到。
    text = f"{chunk.heading}\n{chunk.summary}" if chunk.summary else chunk.text
    if chunk.is_library_document:
        text = f"{text}\n{chunk.text}"
    return tokenize(text)


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.chunks: list[Chunk] = []
        self._tf: list[Counter] = []
        self._lengths: list[int] = []
        self._df: Counter = Counter()
        self._avgdl = 0.0

    def build(self, chunks: list[Chunk]) -> "BM25Index":
        self.chunks = list(chunks)
        self._tf = [Counter(_chunk_tokens(c)) for c in self.chunks]
        self._lengths = [sum(tf.values()) for tf in self._tf]
        self._df = Counter()
        for tf in self._tf:
            self._df.update(tf.keys())
        self._avgdl = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        return self

    def doc_count(self) -> int:
        return len({c.doc_id for c in self.chunks})

    def _idf(self, term: str) -> float:
        n = len(self.chunks)
        df = self._df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, question: str, top_k: int = 6,
               allowed_doc_ids: set[str] | None = None) -> list[ScoredChunk]:
        if not self.chunks:
            return []
        # 覆盖率按 idf 加权，而不是简单计数：「检测」这类通用词 idf 很低，单独命中不等于 100% 覆盖；
        # 「人名」「设备编号」这类专有词 idf 很高，命中即强信号。
        # 知识库里完全没有的词按 20% 权重计入分母：既保留口语多字、错别字不致命，
        # 又不让「问题大半内容都没被记录覆盖」的情况冒充高覆盖。
        terms = list(dict.fromkeys(query_terms(question)))
        idf = {t: self._idf(t) for t in terms}
        total = sum(idf[t] if t in self._df else idf[t] * 0.2 for t in terms) or 1.0
        if not any(t in self._df for t in terms):
            return []

        results: list[ScoredChunk] = []
        for i, tf in enumerate(self._tf):
            # 在候选评分及 top-k 之前限定范围，不能先取全库前几条再删掉无权记录。
            if allowed_doc_ids is not None and self.chunks[i].doc_id not in allowed_doc_ids:
                continue
            matched = [t for t in terms if t in tf]
            if not matched:
                continue
            norm = self.k1 * (1 - self.b + self.b * self._lengths[i] / (self._avgdl or 1))
            score = sum(idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in matched)
            coverage = sum(idf[t] for t in matched) / total
            results.append(ScoredChunk(chunk=self.chunks[i], score=score, coverage=coverage, bm25_used=True))

        results.sort(key=lambda h: (h.score, h.coverage), reverse=True)
        return results[:top_k]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": INDEX_VERSION, "chunks": [c.to_dict() for c in self.chunks]}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        index = cls()
        if not path.is_file():
            return index.build([])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            chunks = [Chunk.from_dict(c) for c in data.get("chunks", [])]
        except (OSError, ValueError, KeyError):
            chunks = []
        return index.build(chunks)
