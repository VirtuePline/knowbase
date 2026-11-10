"""把分块还原成便于阅读的预览结构。

Excel 台账会被切成多块且每块重复表头，预览时需要按工作表合并回去，
否则表格会碎成一段段带重复表头的小表。
"""
from __future__ import annotations

from app.domain.schemas import Chunk

MAX_PREVIEW_CHARS = 60000


def merge_preview(chunks: list[Chunk]) -> list[dict]:
    """按「文档 + 章节」合并表格块，文本块原样保留顺序。"""
    sections: list[dict] = []
    current_table: dict | None = None
    current_key: tuple[str, str] | None = None
    total = 0

    for chunk in chunks:
        if total >= MAX_PREVIEW_CHARS:
            sections.append({"kind": "notice", "text": "预览内容过长，仅显示前一部分，请下载原文件查看完整内容。"})
            break

        if chunk.table:
            key = (chunk.doc_id, chunk.heading)
            rows = [list(row) for row in chunk.table]
            if current_table is not None and current_key == key:
                # 同一张表的后续分块：去掉重复表头后追加
                current_table["table"].extend(rows[1:] if rows else [])
            else:
                current_table = {
                    "kind": "table",
                    "heading": chunk.heading,
                    "location": chunk.location,
                    "table": rows,
                    "summary": chunk.summary.split("\n", 1)[0] if chunk.summary else "",
                }
                current_key = key
                sections.append(current_table)
            total += sum(len("".join(r)) for r in rows)
        else:
            current_table = None
            current_key = None
            sections.append({
                "kind": "text",
                "heading": chunk.heading,
                "location": chunk.location,
                "text": chunk.text,
                "summary": chunk.summary,
            })
            total += len(chunk.text)

    return sections


def is_editable(ext: str) -> bool:
    return ext.lower() in {".md", ".txt"}
