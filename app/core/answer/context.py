"""把命中内容渲染成模型上下文与纯检索摘录。"""
from __future__ import annotations

from app.config import settings
from app.core.retrieval.tokenizer import query_terms
from app.domain.schemas import ScoredChunk

# 表格窗口块的判定宽度：分块时每个窗口默认 excel_rows_per_chunk 行数据 + 表头
_FOCUS_WINDOW = max(1, settings.excel_rows_per_chunk)


def is_stats_question(text: str) -> bool:
    """统计/清单类问题（几台、多少、哪些…）：需要完整台账数据，不适合按词挑行。"""
    lowered = text.lower()
    return any(hint in lowered for hint in _STATS_HINTS)


def _escape(cell: str) -> str:
    return cell.replace("|", "\\|").replace("\n", " ").strip()


def table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    lines = []
    for index, row in enumerate(rows):
        cells = [_escape(c) for c in (list(row) + [""] * width)[:width]]
        lines.append("| " + " | ".join(cells) + " |")
        if index == 0:
            lines.append("|" + "|".join([" --- "] * width) + "|")
    return "\n".join(lines)


def _source_label(hit: ScoredChunk) -> str:
    chunk = hit.chunk
    location = chunk.location or chunk.heading
    return f"《{chunk.doc_name}》{(' · ' + location) if location else ''}"


_STATS_HINTS = ("几台", "几条", "几个", "几项", "几行", "几名", "哪些", "哪几",
                "统计", "汇总", "清单", "列出", "一览", "全部", "所有",
                "多少台", "多少条", "多少个", "多少项", "多少行", "多少名", "多少人")


def _focus_rows(rows: list[list[str]], question: str) -> list[list[str]]:
    """台账类表格：大表只留表头与命中的行，避免整表被字符上限截断后看不到关键行。

    分块后的表格窗口本来就不大（默认 12 行），按关键词挑行容易失真——查询词往往
    只命中表头字段名（"IP"列名），而数据行里是具体值（10.10.x.x 或空），结果只剩
    两行表头。因此窗口内的表整表返回；统计/清单类问题（"几台没有IP"）也整表返回；
    只对超出窗口宽度的大表按词挑行。
    """
    if len(rows) < 3 or not question.strip():
        return rows
    if is_stats_question(question):
        return rows
    if len(rows) <= _FOCUS_WINDOW + 2:   # 窗口块：表头 + 12 行数据以内，全量保留
        return rows
    terms = [t for t in query_terms(question) if len(t) >= 2]
    if not terms:
        return rows
    picked = [row for row in rows[1:]
              if any(t in " ".join(row).lower() for t in terms)]
    if not picked:
        return rows
    return [rows[0], *picked[:8]]


def _body(chunk, question: str = "") -> str:
    if chunk.table:
        return table_to_markdown(_focus_rows(chunk.table, question))
    text = chunk.text.strip()
    if chunk.heading and chunk.heading not in text:
        return f"{chunk.heading}\n{text}"
    return text


def render_hits(hits: list[ScoredChunk], max_chars: int | None = None,
                question: str = "") -> str:
    """模型上下文：每条命中带来源标注，表格转 Markdown 便于逐行取字段。"""
    limit = settings.max_context_chars if max_chars is None else max_chars
    parts: list[str] = []
    used = 0
    for number, hit in enumerate(hits, start=1):
        block = f"【记录 {number}】来源：{_source_label(hit)}\n{_body(hit.chunk, question)}"
        if used + len(block) > limit:
            remaining = limit - used
            if remaining > 200:
                parts.append(block[:remaining] + "\n……（后略）")
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def source_snippet(hit: ScoredChunk, max_chars: int | None = None,
                   question: str = "") -> str:
    """单条命中的摘录，用于前端「依据的文档片段」。"""
    limit = settings.max_excerpt_chars if max_chars is None else max_chars
    body = _body(hit.chunk, question)
    if len(body) > limit:
        body = body[:limit].rstrip() + "……"
    return body


def render_excerpts(hits: list[ScoredChunk], max_chars: int | None = None,
                    question: str = "") -> str:
    """纯检索模式：不做任何推断，按记录逐条列出原文摘录。"""
    parts = []
    for number, hit in enumerate(hits, start=1):
        parts.append(
            f"【记录 {number}】{_source_label(hit)}\n{source_snippet(hit, max_chars, question)}")
    return "\n\n".join(parts)
