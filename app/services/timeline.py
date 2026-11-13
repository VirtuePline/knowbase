"""工作记录时间线，按部门、时间段和分类分页查询；文档库不参与。

全文摘要由文档分析阶段按「时间 → 问题 → 尝试办法 → 结果 → 是否解决 → 原因」的事件记录式生成；
时间线从摘要提取事件日期（提取不到时回退上传时间），数据库完成筛选、排序和统计，
每次仅返回当前页的事件，避免读取全部文档到应用内存。
"""
from __future__ import annotations

import re

from fastapi import HTTPException

from app.domain.taxonomy import all_doc_categories
from app.services import access, database

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


def _padded_capture(match: str, index: int) -> dict:
    capture = {"$arrayElemAt": [f"{match}.captures", index]}
    return {"$cond": [{"$eq": [{"$strLenCP": capture}, 1]}, {"$concat": ["0", capture]}, capture]}


def _date_expression() -> dict:
    """与 _extract_date 相同的摘要日期规则，留在 MongoDB 中供排序和筛选。"""
    year = lambda match: {"$arrayElemAt": [f"{match}.captures", 0]}
    return {"$cond": [
        {"$ne": ["$_tl_full_date", None]},
        {"$concat": [year("$_tl_full_date"), "-", _padded_capture("$_tl_full_date", 1),
                     "-", _padded_capture("$_tl_full_date", 2)]},
        {"$cond": [
            {"$ne": ["$_tl_month_date", None]},
            {"$concat": [year("$_tl_month_date"), "-", _padded_capture("$_tl_month_date", 1)]},
            {"$substrCP": [{"$ifNull": ["$uploaded_at", ""]}, 0, 10]},
        ]},
    ]}


def _status_stages() -> list[dict]:
    """人工状态只在需要的筛选维度或当前页关联，不把全库状态读入 Python。"""
    return [
        {"$lookup": {"from": "event_status", "localField": "_id", "foreignField": "_id",
                     "pipeline": [{"$project": {"status": 1}}], "as": "_tl_manual"}},
        {"$set": {"_tl_status": {"$let": {
            "vars": {"manual": {"$ifNull": [{"$arrayElemAt": ["$_tl_manual.status", 0]}, ""]}},
            "in": {"$cond": [{"$ne": ["$$manual", ""]}, "$$manual", {"$ifNull": ["$resolved", ""]}]},
        }}}},
        {"$set": {"_tl_effective": {"$switch": {
            "branches": [
                {"case": {"$eq": ["$_tl_status", "resolved"]}, "then": "yes"},
                {"case": {"$eq": ["$_tl_status", "improving"]}, "then": "improving"},
                {"case": {"$eq": ["$_tl_status", "temporary"]}, "then": "temporary"},
            ], "default": "no",
        }}}},
    ]


def _record_filter() -> dict:
    """与登记表的存量类型归一规则一致，旧常识库仍按文档库排除。"""
    return {"$or": [
        {"doc_type": "record"},
        {"doc_type": {"$nin": ["doc", "record"]},
         "category": {"$ne": "常识库"}, "categories": {"$ne": "常识库"}},
    ]}


def _pipeline(*, start_date: str, end_date: str, category: str, vendor: str,
              status: str, offset: int, limit: int, department_id: str = "",
              parent_id: str | None = None) -> list[dict]:
    allowed_categories = all_doc_categories()
    stages = [
        {"$match": access.read_filter()},
        {"$match": _record_filter()},
    ]
    if parent_id is None:
        stages.append({"$match": {"status": "ready", "summary": {"$type": "string"}}})
    else:
        # 展开的后续记录不受主时间线的时间范围影响，也展示尚未完成入库的记录。
        stages.append({"$match": {"parent_id": parent_id}})
    stages += [
        {"$project": {"name": 1, "summary": {"$trim": {"input": {"$cond": [
                          {"$eq": [{"$type": "$summary"}, "string"]}, "$summary", ""]}}}, "uploaded_at": 1,
                      "category": 1, "categories": 1, "resolved": 1, "vendor": 1,
                      "doc_type": 1, "parent_id": 1, "root_id": 1, "status": 1, "error": 1,
                      "uploaded_by": 1, "uploaded_by_name": 1, "department_id": 1,
                      "visibility": 1, "shared_department_ids": 1}},
    ]
    if parent_id is None:
        stages.append({"$match": {"summary": {"$ne": ""}}})
    stages += [
        {"$set": {
            "_tl_full_date": {"$regexFind": {"input": "$summary", "regex": _DATE_FULL.pattern}},
            "_tl_month_date": {"$regexFind": {"input": "$summary", "regex": _DATE_MONTH.pattern}},
            "_tl_category": {"$let": {
                "vars": {"name": {"$trim": {"input": {"$ifNull": ["$category", ""]}}}},
                "in": {"$cond": [{"$in": ["$$name", allowed_categories]}, "$$name", "其他"]},
            }},
        }},
        {"$set": {
            "_tl_date": _date_expression(),
            "_tl_department": {"$cond": [
                {"$in": [{"$ifNull": ["$department_id", ""]}, ["", "operations"]]},
                "operations", "$department_id",
            ]},
            "_tl_categories": {"$let": {
                "vars": {"tags": {"$filter": {
                    "input": {"$cond": [{"$isArray": "$categories"}, "$categories", []]},
                    "as": "tag", "cond": {"$in": ["$$tag", allowed_categories]},
                }}},
                "in": {"$cond": [{"$gt": [{"$size": "$$tags"}, 0]}, "$$tags", ["$_tl_category"]]},
            }},
        }},
    ]
    if start_date or end_date:
        # 只有月份的记录覆盖整月，与选择的日期段有交集即可纳入；展示仍保留年月精度。
        stages.append({"$set": {
            "_tl_range_start": {"$cond": [{"$eq": [{"$strLenCP": "$_tl_date"}, 7]},
                                             {"$concat": ["$_tl_date", "-01"]}, "$_tl_date"]},
            "_tl_range_end": {"$cond": [{"$eq": [{"$strLenCP": "$_tl_date"}, 7]},
                                           {"$concat": ["$_tl_date", "-31"]}, "$_tl_date"]},
        }})
        date_match: dict = {"_tl_date": {"$ne": ""}}
        if start_date:
            date_match["_tl_range_end"] = {"$gte": start_date}
        if end_date:
            date_match["_tl_range_start"] = {"$lte": end_date}
        stages.append({"$match": date_match})

    department_scope = [{"$match": {"_tl_department": department_id}}] if department_id else []
    category_match = {"_tl_categories": category} if category else {}
    selection = [*department_scope, {"$match": category_match}]
    if vendor:
        selection.append({"$match": {"vendor": vendor}})
    filtered = list(selection)
    if status:
        filtered += [*_status_stages(),
                     {"$match": {"_tl_effective": status}}]
    page = [*filtered, {"$sort": {"_tl_date": -1, "name": -1, "_id": -1}},
            {"$skip": offset}, {"$limit": limit}]
    if not status:
        page += _status_stages()
    # 先分页再补父名称与追踪数；失败/处理中的可见子记录也能展开查看。
    page += [
        {"$lookup": {"from": "documents", "localField": "parent_id", "foreignField": "_id",
                     "pipeline": [{"$match": access.read_filter()}, {"$project": {"name": 1}}], "as": "_tl_parent"}},
        {"$lookup": {"from": "documents", "localField": "root_id", "foreignField": "_id",
                     "pipeline": [{"$match": access.read_filter()}, {"$project": {"_id": 1}}], "as": "_tl_root"}},
        {"$lookup": {"from": "documents", "let": {"parent": "$_id"}, "pipeline": [
            {"$match": access.read_filter()},
            {"$match": _record_filter()},
            {"$match": {"$expr": {"$eq": ["$parent_id", "$$parent"]}}}, {"$count": "count"},
        ], "as": "_tl_children"}},
        {"$set": {
            "_tl_parent_name": {"$ifNull": [{"$arrayElemAt": ["$_tl_parent.name", 0]}, ""]},
            "_tl_child_count": {"$ifNull": [{"$arrayElemAt": ["$_tl_children.count", 0]}, 0]},
            "_tl_root_id": {"$ifNull": [{"$arrayElemAt": ["$_tl_root._id", 0]}, "$_id"]},
        }},
        {"$unset": ["_tl_full_date", "_tl_month_date", "_tl_manual", "_tl_parent", "_tl_root", "_tl_children",
                     "_tl_range_start", "_tl_range_end"]},
    ]
    if parent_id is not None:
        # 展开列表只需当前页和总数，不重复统计子记录的所有筛选维度。
        stages.append({"$facet": {"events": page, "total": [{"$count": "count"}]}})
        return stages
    stages.append({"$facet": {
        "events": page,
        "total": [*filtered, {"$count": "count"}],
        "departments": [{"$group": {"_id": "$_tl_department", "count": {"$sum": 1}}}],
        "event_count": [*department_scope, {"$count": "count"}],
        "categories": [*department_scope, {"$unwind": "$_tl_categories"},
                       {"$group": {"_id": "$_tl_categories", "count": {"$sum": 1}}}],
        "issue_count": [*department_scope, {"$count": "count"}],
        "vendors": [*department_scope, {"$match": category_match}, {"$match": {"vendor": {"$nin": ["", None]}}},
                    {"$group": {"_id": "$vendor", "count": {"$sum": 1}}}, {"$sort": {"_id": 1}}],
        "status_counts": [*selection, *_status_stages(),
                          {"$group": {"_id": "$_tl_effective", "count": {"$sum": 1}}}],
    }})
    return stages


def build(*, start_date: str = "", end_date: str = "", category: str = "", vendor: str = "",
          status: str = "", offset: int = 0, limit: int = 30, department_id: str = "") -> dict:
    """每次只返回最新一页；时间段和各项筛选、计数都在数据库中完成。"""
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("分页参数无效")
    if status not in {"", "yes", "no", "improving", "temporary"}:
        raise ValueError("完成状态无效")
    if start_date and end_date and start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    result = database.timeline_page(_pipeline(
        start_date=start_date, end_date=end_date, category=category, vendor=vendor,
        status=status, offset=offset, limit=limit, department_id=department_id,
    ))
    return _page_result(result, offset)


def children(doc_id: str, *, offset: int = 0, limit: int = 20) -> dict:
    """按权限分页读取直接后续记录；每条子记录可继续展开自己的后续。"""
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("分页参数无效")
    parent = access.require_read(doc_id)
    if parent.doc_type != "record":
        raise HTTPException(status_code=404, detail="工作记录不存在或无权访问")
    result = database.timeline_page(_pipeline(
        start_date="", end_date="", category="", vendor="", status="",
        offset=offset, limit=limit, parent_id=doc_id,
    ))
    payload = _page_result(result, offset)
    return {key: payload[key] for key in ("events", "total", "has_more", "next_offset")}


def _page_result(result: dict, offset: int) -> dict:
    department_counts = {row["_id"]: row["count"] for row in result.get("departments", []) if row.get("_id")}
    names = database.department_names(
        set(department_counts) | {row.get("_tl_department") or row.get("department_id") or "operations"
                                  for row in result.get("events", [])}
    )
    events = []
    for row in result.get("events", []):
        record = database._row_to_doc(row)
        event_status = row.get("_tl_status", "")
        events.append({
            "chunk_id": record.id, "date": row["_tl_date"],
            "category": row["_tl_category"], "categories": row["_tl_categories"],
            "doc_id": record.id, "doc_name": record.name, "doc_type": record.doc_type,
            "vendor": record.vendor, "uploaded_by": record.uploaded_by,
            "uploaded_by_name": database.recorder_label(record.uploaded_by, record.uploaded_by_name),
            "parent_id": record.parent_id if row.get("_tl_parent_name") else "",
            "root_id": row.get("_tl_root_id") or record.id, "parent_name": row.get("_tl_parent_name", ""),
            "child_count": row.get("_tl_child_count", 0), "brief": _brief(record.summary),
            "heading": "", "summary": record.summary,
            "ingest_status": record.status, "error": record.error,
            "resolved": "yes" if event_status == "resolved" else "no", "status": event_status,
            "can_write": access.can_write(record), "department_id": record.department_id,
            "department_name": names.get(record.department_id, "未命名部门"),
            "visibility": record.visibility, "shared_department_ids": record.shared_department_ids,
        })
    counts = {row["_id"]: row["count"] for row in result.get("categories", []) if row.get("_id")}
    category_order = all_doc_categories() + sorted(set(counts) - set(all_doc_categories()))
    total = next((row["count"] for row in result.get("total", [])), 0)
    statuses = {name: 0 for name in ("yes", "no", "improving", "temporary")}
    statuses.update({row["_id"]: row["count"] for row in result.get("status_counts", [])})
    next_offset = offset + len(events)
    return {
        "events": events,
        "departments": sorted(
            [{"id": key, "name": names[key], "count": count} for key, count in department_counts.items()],
            key=lambda item: (item["name"], item["id"]),
        ),
        "event_count": next((row["count"] for row in result.get("event_count", [])), 0),
        "categories": [{"name": name, "count": counts[name]} for name in category_order if counts.get(name)],
        "issue_count": next((row["count"] for row in result.get("issue_count", [])), 0),
        "vendors": [{"name": row["_id"], "count": row["count"]} for row in result.get("vendors", [])],
        "status_counts": {"": sum(statuses.values()), **statuses},
        "total": total, "has_more": next_offset < total, "next_offset": next_offset,
    }
