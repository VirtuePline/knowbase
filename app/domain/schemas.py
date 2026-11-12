"""跨层共享的数据结构，不依赖框架。"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any


@dataclass
class Block:
    """解析器输出的原始文本块。表格类内容额外保留二维表格。"""
    text: str
    location: str = ""
    heading: str = ""
    table: list[list[str]] | None = None
    summary: str = ""           # 文档分析阶段生成的摘要；为空表示未经分析


@dataclass
class Chunk:
    """进入检索的最小单元。

    检索只看 summary（为空时退回 text）；text / table 是原文，作为元数据随命中返回，
    供模型作答与前端展示。
    """
    id: str
    doc_id: str
    doc_name: str
    doc_type: str
    location: str
    text: str
    heading: str = ""
    table: list[list[str]] | None = None
    summary: str = ""
    category: str = ""          # 所属文档分类，随分块保存，便于后续按分类过滤检索
    references: list[str] = field(default_factory=list)  # 命中后补充来源与模型材料，不参与评分

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "doc_type": self.doc_type,
            "location": self.location,
            "text": self.text,
            "heading": self.heading,
            "table": self.table,
            "summary": self.summary,
            "category": self.category,
            "references": self.references,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Chunk":
        return cls(
            id=data["id"],
            doc_id=data["doc_id"],
            doc_name=data["doc_name"],
            doc_type=data.get("doc_type", ""),
            location=data.get("location", ""),
            text=data["text"],
            heading=data.get("heading", ""),
            table=data.get("table"),
            summary=data.get("summary", ""),
            category=data.get("category", ""),
            references=data.get("references", []),
        )


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float
    coverage: float
    semantic: float = 0.0     # 向量余弦相似度；是否计算见 semantic_used
    rerank: float = 0.0       # Cross-Encoder 分值；是否计算见 rerank_used
    bm25_used: bool = False
    semantic_used: bool = False
    rerank_used: bool = False
    context_only: bool = False  # 完整台账扩展片段，未对本次问题单独评分


@dataclass
class SourceRef:
    doc_id: str
    doc_name: str
    location: str
    score: float
    snippet: str
    table: list[list[str]] | None = None
    summary: str = ""
    kind: str = "knowledge_base"  # "knowledge_base" | "web"
    url: str = ""                # 联网参考的原始 HTTP(S) 地址
    scoring_method: str = ""       # rrf / web / context / reference；空表示旧来源未注明
    uploaded_at: str = ""          # 来源文档的上传时间（YYYY-MM-DD HH:MM:SS）；展示取年月日
    rrf_score: float | None = None
    keyword_coverage: float | None = None
    semantic_similarity: float | None = None
    rerank_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        def metric(value: float | None) -> float | None:
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                return None
            return round(value, 6)

        return {
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "location": self.location,
            "score": round(self.score, 3),
            "snippet": self.snippet,
            "table": self.table,
            "summary": self.summary,
            "kind": self.kind,
            "url": self.url,
            "scoring_method": self.scoring_method,
            "uploaded_at": self.uploaded_at,
            "rrf_score": metric(self.rrf_score),
            "keyword_coverage": metric(self.keyword_coverage),
            "semantic_similarity": metric(self.semantic_similarity),
            "rerank_score": metric(self.rerank_score),
        }


@dataclass
class AnswerResponse:
    status: str                 # "ok" | "not_found" | "guide" | "error"
    answer: str
    category: str = ""
    sources: list[SourceRef] = field(default_factory=list)
    notice: str = ""
    llm_used: bool = False
    answer_origin: str = "knowledge_base"  # "knowledge_base" | "web" | "none"
    solution_found: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "answer": self.answer,
            "category": self.category,
            "sources": [s.to_dict() for s in self.sources],
            "notice": self.notice,
            "llm_used": self.llm_used,
            "answer_origin": self.answer_origin,
            "solution_found": self.solution_found,
        }


@dataclass
class DocRecord:
    id: str
    name: str
    stored_name: str
    ext: str
    size: int
    sha256: str
    chunks: int
    uploaded_at: str
    status: str
    error: str = ""
    summary: str = ""           # 全文摘要
    skipped: int = 0            # 分析时判定为非主要内容、未进入检索的部分数
    category: str = ""          # 主分类：自动判定或人工选择的首个分类；多标签时与 categories[0] 一致
    categories: list[str] = field(default_factory=list)  # 全部分类标签（一条记录可挂多个）；空时回退 [category]
    vendor: str = ""            # 厂商（厂商系统类记录按厂商归类）
    doc_type: str = ""          # doc=基础文档（手册 / 资料等）；record=运维问题记录；空为存量兼容（读取时按常识库→doc、其余→record 补齐）
    resolved: str = ""          # 录入时选择的解决状态：unresolved=未解决 / improving=处理中 / temporary=临时解决 / resolved=已解决决；空为未标记（默认未解决）
    uploaded_by: str = ""       # 创建记录时的登录账号；旧数据 / 命令行可为空
    uploaded_by_name: str = ""  # 运维人用户名（默认当前登录用户名，可改）；旧数据为空时展示回退账号
    parent_id: str = ""         # 所跟踪的上一条运维记录，创建后不变
    root_id: str = ""           # 跟踪链的根记录；旧数据为空时按自身 ID 处理
    last_edited_by: str = ""    # 最近一次在线编辑的登录账号；溯源用
    last_edited_name: str = ""  # 最近一次在线编辑的显示名
    last_edited_at: str = ""    # 最近一次在线编辑时间

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ext": self.ext,
            "size": self.size,
            "chunks": self.chunks,
            "uploaded_at": self.uploaded_at,
            "status": self.status,
            "error": self.error,
            "summary": self.summary,
            "skipped": self.skipped,
            "category": self.category,
            "categories": list(self.categories or []),
            "vendor": self.vendor,
            "doc_type": self.doc_type,
            "resolved": self.resolved,
            "uploaded_by": self.uploaded_by,
            "uploaded_by_name": self.uploaded_by_name,
            "parent_id": self.parent_id,
            "root_id": self.root_id,
            "last_edited_by": self.last_edited_by,
            "last_edited_name": self.last_edited_name,
            "last_edited_at": self.last_edited_at,
        }
