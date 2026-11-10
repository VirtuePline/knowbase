"""切块：表格按行窗口（重复表头），正文按段落累积并带重叠。

经过文档分析的 Block（summary 非空）不再按字数细切：每个结构段整体作为一条检索单元，
摘要用于检索，章节原文作为元数据保留。表格仍按行窗口切分，但摘要只描述表名、字段和
记录数量，不把每条明细写入摘要。
"""
from __future__ import annotations

import hashlib
import re

from app.config import settings
from app.domain.schemas import Block, Chunk

_SENTENCE_END = re.compile(r"(?<=[。！？；!?;\n])")


def _chunk_id(doc_id: str, seq: int) -> str:
    return hashlib.sha1(f"{doc_id}:{seq}".encode("utf-8")).hexdigest()[:16]


def is_title_row(rows: list[list[str]]) -> bool:
    """首行只有一个非空单元格、次行有多个字段：首行是表标题而非表头。

    台账 Excel 常见结构：第 1 行是合并标题（如「主机、显示器台账」），第 2 行才是
    字段名（序号、品名、IP…）。固定拿第一行当表头会把标题当字段、把字段行当数据。
    """
    if len(rows) < 2:
        return False
    first = [str(cell).strip() for cell in rows[0] if str(cell).strip()]
    second = [str(cell).strip() for cell in rows[1] if str(cell).strip()]
    return len(first) <= 1 and len(second) >= 2


def _table_text(rows: list[list[str]]) -> str:
    return "\n".join(" | ".join(c for c in row) for row in rows)


def split_table(block: Block, doc: dict, seq: int) -> list[Chunk]:
    """表格按行窗口切分，每块重复表头。行号按工作表的绝对行号（数据从表头下一行开始）。

    首行是合并标题时，标题并入分块标题（heading），表头取第二行。
    """
    rows = block.table or []
    if len(rows) < 2:  # 只有一行时没有表头可言，当作普通文本
        flat = Block(text=_table_text(rows), heading=block.heading, location=block.location)
        return _text_chunks(flat, doc, seq)

    title = ""
    if is_title_row(rows):
        title = " ".join(str(cell).strip() for cell in rows[0] if str(cell).strip())
        rows = rows[1:]
    header, body = rows[0], rows[1:]
    heading = f"{block.heading}（{title}）" if title else block.heading
    window = max(1, settings.excel_rows_per_chunk)
    chunks: list[Chunk] = []
    for start in range(0, len(body), window):
        part = body[start:start + window]
        first_line = start + (3 if title else 2)   # 数据行在原表中的行号：标题 + 表头占前两行
        last_line = first_line + len(part) - 1
        piece = [header, *part]
        location = f"{block.location} 第{first_line}-{last_line}行".strip()
        summary = _table_window_summary(block.summary, header, part) if block.summary else ""
        chunks.append(Chunk(
            id=_chunk_id(doc["id"], seq + len(chunks)),
            doc_id=doc["id"], doc_name=doc["name"], doc_type=doc["type"],
            location=location, text=_table_text(piece), heading=heading, table=piece,
            summary=summary, category=doc.get("category", ""),
        ))
    return chunks


def _table_window_summary(table_summary: str, header: list[str], rows: list[list[str]]) -> str:
    """表格摘要只描述结构和记录范围，不罗列每行日期、人员、编号等明细。"""
    return f"{table_summary}；本段包含 {len(rows)} 条明细，具体字段值保留在原文中"


def _split_text(text: str, size: int, overlap: int) -> list[str]:
    """先按句切，再累积到 size 以内；块之间保留约 overlap 字的尾部上下文。"""
    sentences = [s for s in _SENTENCE_END.split(text) if s.strip()]
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > size:                 # 超长单句硬切
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:size])
            sentence = sentence[size - overlap:] if overlap < size else ""
        if len(current) + len(sentence) <= size:
            current += sentence
        else:
            pieces.append(current)
            tail = current[-overlap:] if overlap > 0 else ""
            current = tail + sentence if len(tail) + len(sentence) <= size else sentence
    if current.strip():
        pieces.append(current)
    return [p.strip() for p in pieces if p.strip()]


def _text_chunks(block: Block, doc: dict, seq: int) -> list[Chunk]:
    pieces = _split_text(block.text, settings.chunk_size, settings.chunk_overlap)
    chunks: list[Chunk] = []
    for index, piece in enumerate(pieces):
        location = block.location
        if len(pieces) > 1:
            location = f"{location} 第{index + 1}段".strip()
        chunks.append(Chunk(
            id=_chunk_id(doc["id"], seq + index),
            doc_id=doc["id"], doc_name=doc["name"], doc_type=doc["type"],
            location=location, text=piece, heading=block.heading,
            category=doc.get("category", ""),
        ))
    return chunks


def _section_chunk(block: Block, doc: dict, seq: int) -> Chunk:
    return Chunk(
        id=_chunk_id(doc["id"], seq),
        doc_id=doc["id"], doc_name=doc["name"], doc_type=doc["type"],
        location=block.location, text=block.text.strip(), heading=block.heading,
        summary=block.summary, category=doc.get("category", ""),
    )


def build_chunks(blocks: list[Block], doc: dict) -> list[Chunk]:
    chunks: list[Chunk] = []
    for block in blocks:
        if not block.text.strip() and not block.table:
            continue
        if block.table:
            chunks.extend(split_table(block, doc, len(chunks)))
        elif block.summary:
            chunks.append(_section_chunk(block, doc, len(chunks)))
        else:
            chunks.extend(_text_chunks(block, doc, len(chunks)))
    return chunks
