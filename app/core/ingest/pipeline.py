"""入库流水线与索引重建。

原始文件 → Block → Chunk → data/index/chunks/<doc_id>.json → BM25 索引。
分块单独落盘，删除文档或重建索引时不必重新解析原始文件。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.core.ingest.analyzer import analyze
from app.core.ingest.chunker import build_chunks
from app.core.ingest.loaders import LoaderError, load_blocks, reference_ids
from app.core.retrieval.bm25 import BM25Index
from app.domain.schemas import Chunk
from app.services import database


@dataclass
class IngestResult:
    ok: bool
    chunks: int = 0
    error: str = ""
    summary: str = ""
    skipped: int = 0
    category: str = ""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunk_path(doc_id: str) -> Path:
    return settings.chunks_dir / f"{doc_id}.json"


class Ingester:
    def ingest(self, path: Path, doc_id: str, name: str, doc_type: str,
               category: str = "", *, content_type: str = "record") -> IngestResult:
        try:
            raw_blocks = load_blocks(path)
            references = reference_ids(path)
        except LoaderError as exc:
            return IngestResult(ok=False, error=str(exc))
        except Exception as exc:
            return IngestResult(ok=False, error=f"解析失败：{type(exc).__name__}: {exc}")

        try:
            doc_analysis = analyze(raw_blocks, name, classify_category=content_type != "doc")
            analyzed_blocks = doc_analysis.sections
        except Exception as exc:
            return IngestResult(ok=False, error=f"文档分析失败：{type(exc).__name__}: {exc}")

        try:
            chunks = build_chunks(analyzed_blocks, {
                "id": doc_id, "name": name, "type": doc_type,
                "category": "" if content_type == "doc" else category or doc_analysis.category,
            })
        except Exception as exc:
            return IngestResult(ok=False, error=f"切块失败：{type(exc).__name__}: {exc}")

        if not chunks:
            return IngestResult(ok=False, error="未能切分出有效内容")

        for chunk in chunks:
            chunk.references = [ref for ref in references if ref != doc_id]
            chunk.content_type = content_type
        self._write(doc_id, chunks)
        return IngestResult(
            ok=True, chunks=len(chunks),
            summary=doc_analysis.summary if doc_analysis else "",
            skipped=doc_analysis.skipped if doc_analysis else 0,
            category="" if content_type == "doc" else category or (doc_analysis.category if doc_analysis else ""),
        )

    def _write(self, doc_id: str, chunks: list[Chunk]) -> None:
        settings.chunks_dir.mkdir(parents=True, exist_ok=True)
        path = _chunk_path(doc_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps([c.to_dict() for c in chunks], ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def read_chunks(self, doc_id: str) -> list[Chunk]:
        path = _chunk_path(doc_id)
        if not path.is_file():
            return []
        try:
            return [Chunk.from_dict(c) for c in json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, KeyError):
            return []

    def remove(self, doc_id: str) -> None:
        _chunk_path(doc_id).unlink(missing_ok=True)


def rebuild_index() -> BM25Index:
    """从登记表中所有 ready 文档的分块文件重建索引。"""
    ingester = Ingester()
    chunks: list[Chunk] = []
    known = set()
    for record in database.list_all():
        known.add(record.id)
        if record.status == "ready":
            for chunk in ingester.read_chunks(record.id):
                # 业务类型以登记表为准；旧文档重建后仍可按原文中的字段值检索。
                chunk.content_type = record.doc_type
                if record.doc_type == "doc":
                    chunk.category = ""
                elif record.category and chunk.category != record.category:
                    chunk.category = record.category
                chunks.append(chunk)

    # 清理登记表里已不存在的孤儿分块文件（处理中的文档仍在登记表内，不受影响）
    if settings.chunks_dir.is_dir():
        for path in settings.chunks_dir.glob("*.json"):
            if path.stem not in known:
                path.unlink(missing_ok=True)

    index = BM25Index().build(chunks)
    index.save(settings.index_file)
    return index
