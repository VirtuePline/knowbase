"""按分类的问题时间线：每份已入库文档一条事件记录，与文档管理页数量一致。

全文摘要由文档分析阶段按「时间 → 问题 → 尝试办法 → 结果 → 是否解决 → 原因」的事件记录式生成；
时间线从每份文档的全文摘要中提取事件日期（提取不到时回退文档上传时间），按分类分组、按时间排序，
直观呈现每个分类下何时遇到什么问题、何时解决了什么问题。
"""
from __future__ import annotations

import re
from collections import Counter

from app.domain.taxonomy import DOC_CATEGORY_ORDER, normalize_doc_category, record_categories
from app.services import database

# 摘要中的日期写法 → 归一化为 YYYY-MM-DD；只写年月时归一化为 YYYY-MM
_DATE_FULL = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
_DATE_MONTH = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")

# 摘要开头的日期 / 时间前缀，简述里去掉（含「2026年11月6日15时30分」式中文写法）
_TIME_PREFIX = re.compile(
    r"^\s*(?:20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?)?"
    r"(?:\s*\d{1,2}[时点]\d{1,2}分?|\s*\d{1,2}:\d{2}(?::\d{2})?)?\s*"
)

# 「现象：xxx / 问题（遇到什么问题）：xxx」式的事件记录写法，直接取现象段做简述
_EVENT_BRIEF = re.compile(r"(?:现象|问题)(?:（遇到什么问题）)?[：:]\s*([^。；\n]{2,60}?)(?=，|。|；|处理过程|$)")

BRIEF_LIMIT = 15   # 时间线每条问题的简述字数上限


def _brief(summary: str) -> str:
    """从摘要中提取不超过 15 个字的问题简述（去掉日期开头，取第一个短句）。"""
    match = _EVENT_BRIEF.search(summary)
    text = match.group(1).strip() if match else ""
    if not text:
        text = _TIME_PREFIX.sub("", summary)
        text = text.lstrip("，,、：:；。 \t")
        text = re.split(r"[。；;\n，]", text)[0].strip()
    text = re.sub(r"^(?:现象|问题)[：:]?", "", text).strip()
    if len(text) > BRIEF_LIMIT:
        text = text[:BRIEF_LIMIT - 1].rstrip() + "…"
    return text


def _extract_date(text: str) -> str:
    match = _DATE_FULL.search(text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = _DATE_MONTH.search(text)
    return f"{match.group(1)}-{int(match.group(2)):02d}" if match else ""


def build() -> dict:
    """每份 ready 状态的文档生成一条事件，按时间倒序（最新在前）返回。

    与文档管理页同步：登记表里仍存在的文档才进入时间线，文档删除时其事件状态一并清理。
    人工标记以文档 ID 为粒度，复用 chunk_id 字段作为键，接口格式保持不变。
    """
    manual = database.get_event_status_map()
    events: list[dict] = []
    records = database.list_all()
    by_id = {record.id: record for record in records}
    children = Counter(record.parent_id for record in records if record.parent_id)
    for record in records:
        if record.status != "ready":
            continue
        summary = (record.summary or "").strip()
        if not summary:
            continue
        category = normalize_doc_category(record.category or "其他")
        tags = record_categories(record)   # 多标签记录的全部分类；主分类始终包含在内
        status = manual.get(record.id, "") or record.resolved
        events.append({
            "chunk_id": record.id,
            "date": _extract_date(summary) or record.uploaded_at[:10],
            "category": category,
            "categories": tags,
            "doc_id": record.id,
            "doc_name": record.name,
            "doc_type": record.doc_type,
            "vendor": record.vendor,
            "uploaded_by": record.uploaded_by,
            "uploaded_by_name": record.uploaded_by_name,
            "parent_id": record.parent_id,
            "root_id": record.root_id or record.id,
            "parent_name": by_id[record.parent_id].name if record.parent_id in by_id else "",
            "child_count": children[record.id],
            "brief": _brief(summary),
            "heading": "",
            "summary": summary,
            # 解决状态：时间线上的人工标记优先，其次录入时选择的「是否解决」，最后默认未解决
            "resolved": "yes" if status == "resolved" else "no",
            "status": status,
        })
    events.sort(key=lambda e: (e["date"], e["doc_name"]), reverse=True)
    counts = Counter(tag for event in events for tag in event["categories"])   # 多标签逐个计数
    categories = [{"name": name, "count": counts[name]} for name in DOC_CATEGORY_ORDER if counts[name]]
    return {"events": events, "categories": categories}
