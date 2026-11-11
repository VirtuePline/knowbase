"""文档登记表（MongoDB）。只存元数据，正文与分块落在文件里。

集合与原 SQLite 表的对应关系：
- documents       文档登记（_id = 文档 id）
- event_status    时间线事件人工状态（_id = chunk_id）
- vendors         厂商清单（_id = name）
- doc_templates   问题类型模板（_id = category）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.domain.schemas import DocRecord
from app.services import mongo

_INT_FIELDS = {"size", "chunks", "skipped"}
_LIST_FIELDS = {"categories"}
_DOC_FIELDS = (
    "id", "name", "stored_name", "ext", "size", "sha256", "chunks",
    "uploaded_at", "status", "error", "summary", "skipped", "category", "categories", "vendor", "doc_type",
    "resolved", "uploaded_by", "uploaded_by_name", "parent_id", "root_id",
    "last_edited_by", "last_edited_name", "last_edited_at",
)


def _normalize_doc_type(row: dict) -> str:
    """存量数据没有 doc_type 字段：常识库归基础文档，其余（历史运维记录）归运维问题记录。"""
    value = str(row.get("doc_type") or "")
    if value in {"doc", "record"}:
        return value
    tags = row.get("categories") or []
    return "doc" if ("常识库" in tags or str(row.get("category") or "") == "常识库") else "record"


def _docs():
    return mongo.db().documents


def init_db() -> None:
    """建索引（幂等）并验证连通性，启动时连不上立即报错。"""
    mongo.ping()
    _docs().create_index([("category", ASCENDING)])
    _docs().create_index([("sha256", ASCENDING)])
    _docs().create_index([("parent_id", ASCENDING)])
    _docs().create_index([("root_id", ASCENDING)])
    mongo.db().event_status.create_index([("doc_id", ASCENDING)])


def _row_to_doc(row: dict) -> DocRecord:
    row = dict(row)
    # 文档主键存 _id；兼容带冗余 id 字段的历史数据
    row["id"] = str(row.get("_id") or row.get("id") or "")
    row["doc_type"] = _normalize_doc_type(row)
    values = {}
    for f in _DOC_FIELDS:
        if f in _INT_FIELDS:
            values[f] = int(row.get(f) or 0)
        elif f in _LIST_FIELDS:
            tags = row.get(f)
            values[f] = [str(t) for t in tags if str(t)] if isinstance(tags, list) else []
        else:
            values[f] = str(row.get(f) or "")
    return DocRecord(**values)


def add(doc: DocRecord) -> None:
    _docs().replace_one({"_id": doc.id}, {"_id": doc.id, **{f: getattr(doc, f) for f in _DOC_FIELDS}}, upsert=True)


def update_status(doc_id: str, chunks: int, status: str, error: str = "",
                  summary: str = "", skipped: int = 0, category: str = "") -> None:
    """写回分析结果。category 为空时保留原分类（失败重试不丢已有结果）。"""
    values: dict = {"chunks": int(chunks), "status": status, "error": error,
                    "summary": summary, "skipped": int(skipped)}
    if category:
        values["category"] = category
    _docs().update_one({"_id": doc_id}, {"$set": values})


def set_category(doc_id: str, category: str) -> bool:
    """人工修正分类。"""
    return _docs().update_one({"_id": doc_id}, {"$set": {"category": category}}).matched_count > 0


def set_categories(doc_id: str, categories: list) -> bool:
    """设置全部分类标签；非空时首标签即主分类（与 category 字段保持一致）。"""
    tags = [str(t).strip() for t in (categories or []) if str(t).strip()]
    values = {"categories": tags}
    if tags:
        values["category"] = tags[0]
    return _docs().update_one({"_id": doc_id}, {"$set": values}).matched_count > 0


def set_vendor(doc_id: str, vendor: str) -> bool:
    """设置 / 清除文档的厂商（厂商系统类记录按厂商归类）。"""
    return _docs().update_one({"_id": doc_id}, {"$set": {"vendor": vendor}}).matched_count > 0


def set_uploaded_at(doc_id: str, value: str) -> bool:
    """人工修正记录时间（列表显示 / 时间线排序用）。"""
    return _docs().update_one({"_id": doc_id}, {"$set": {"uploaded_at": value}}).matched_count > 0


def set_resolved(doc_id: str, value: str) -> bool:
    """设置录入时的解决状态（unresolved / improving / resolved）；空串清除标记。"""
    return _docs().update_one({"_id": doc_id}, {"$set": {"resolved": value}}).matched_count > 0


def set_stored_name(doc_id: str, stored_name: str) -> None:
    _docs().update_one({"_id": doc_id}, {"$set": {"stored_name": stored_name}})


# ---------- 运行时设置（管理端开关，存 app_settings 集合） ----------

def get_runtime_setting(key: str, default: str = "") -> str:
    try:
        row = mongo.db().app_settings.find_one({"_id": key})
    except Exception:   # noqa: BLE001 数据库异常不阻断问答
        return default
    return str((row or {}).get("value") or default)


def set_runtime_setting(key: str, value: str) -> None:
    mongo.db().app_settings.replace_one(
        {"_id": key},
        {"_id": key, "value": value, "updated_at": now_iso()},
        upsert=True,
    )


def web_search_runtime_enabled() -> bool:
    """联网搜索运行时开关：默认关闭（知识库优先）；管理端开启后立即生效，无需重启。"""
    return get_runtime_setting("web_search", "off") == "on"


def fast_mode_enabled() -> bool:
    """快速回答开关：默认开启。

    开启后检索跳过 Cross-Encoder 精排、生成跳过模型调用，命中知识库即回原文方案，
    单次问答在 0.2s 量级；关闭后恢复精排 + 模型整理步骤的高质量路径（数秒）。
    """
    return get_runtime_setting("fast_mode", "on") != "off"


# ---------- 答案否决惩罚（问答页「重新回答」时计入） ----------

def add_answer_penalties(doc_ids, question: str = "") -> int:
    """记录被否决的答案来源文档：按文档累计惩罚次数，后续检索对其降权。"""
    added = 0
    for doc_id in {str(d).strip() for d in doc_ids or [] if str(d).strip()}:
        mongo.db().answer_penalties.update_one(
            {"_id": doc_id},
            {"$inc": {"count": 1},
             "$set": {"last_question": (question or "")[:200], "updated_at": now_iso()}},
            upsert=True,
        )
        added += 1
    return added


def answer_penalty_counts(doc_ids) -> dict:
    """查询文档累计的惩罚次数；仅返回有记录的文档。"""
    ids = [str(d).strip() for d in doc_ids or [] if str(d).strip()]
    if not ids:
        return {}
    return {d["_id"]: int(d.get("count", 0))
            for d in mongo.db().answer_penalties.find({"_id": {"$in": ids}}, {"count": 1})}


# ---------- 文档编辑日志（溯源：谁在何时新建 / 修改过记录） ----------

def add_edit_log(doc_id: str, editor: str, editor_name: str = "", action: str = "修改") -> None:
    mongo.db().edit_logs.insert_one({
        "doc_id": doc_id, "editor": editor, "editor_name": editor_name,
        "action": action, "edited_at": now_iso(),
    })


def list_edit_logs(doc_id: str, limit: int = 20) -> list[dict]:
    rows = (mongo.db().edit_logs.find({"doc_id": doc_id}, {"_id": 0})
            .sort("edited_at", DESCENDING).limit(limit))
    return [{"editor": r.get("editor", ""), "editor_name": r.get("editor_name", ""),
             "action": r.get("action", ""), "edited_at": r.get("edited_at", "")} for r in rows]


def uploaded_dates(doc_ids) -> dict[str, str]:
    """批量查询文档上传时间（doc_id -> YYYY-MM-DD HH:MM:SS）；用于回答来源展示。"""
    ids = [str(d).strip() for d in doc_ids or [] if str(d).strip()]
    if not ids:
        return {}
    return {d["_id"]: str(d.get("uploaded_at", ""))
            for d in mongo.db().documents.find({"_id": {"$in": list(set(ids))}}, {"uploaded_at": 1})
            if d.get("uploaded_at")}


def list_all_edit_logs(limit: int = 100) -> list[dict]:
    """全库编辑溯源日志（新在前），附带记录名称与类型便于展示跳转。"""
    rows = list(mongo.db().edit_logs.find({}, {"_id": 0}).sort("edited_at", DESCENDING).limit(limit))
    doc_ids = {r.get("doc_id", "") for r in rows if r.get("doc_id")}
    docs = {d["_id"]: d for d in mongo.db().documents.find(
        {"_id": {"$in": list(doc_ids)}}, {"name": 1, "doc_type": 1})}
    for r in rows:
        doc = docs.get(r.get("doc_id", ""), {})
        r["doc_name"] = doc.get("name", "")
        r["doc_type"] = doc.get("doc_type", "")
    return rows


# ---------- 运维周报（定时生成，按运维人汇总本周记录标题） ----------

def list_record_owners() -> list[str]:
    """有运维记录的运维人名单（去重），供周报设置勾选。"""
    try:
        names = mongo.db().documents.distinct(
            "uploaded_by_name", {"doc_type": "record", "uploaded_by_name": {"$nin": ["", None]}})
    except Exception:   # noqa: BLE001
        return []
    return sorted({str(n).strip() for n in names if str(n).strip()})


def save_weekly_report(week_start: str, person: str, items: list[dict], generated_at: str,
                       markdown: str = "") -> None:
    """写入 / 覆盖某人本周的周报（_id = 周起始日:运维人，天然幂等）。"""
    mongo.db().weekly_reports.replace_one(
        {"_id": f"{week_start}:{person}"},
        {"_id": f"{week_start}:{person}", "week_start": week_start, "person": person,
         "items": items, "markdown": markdown, "generated_at": generated_at},
        upsert=True,
    )


def has_weekly_report(week_start: str, person: str) -> bool:
    return mongo.db().weekly_reports.find_one({"_id": f"{week_start}:{person}"}) is not None


def list_weekly_reports(limit: int = 12) -> list[dict]:
    rows = (mongo.db().weekly_reports.find({}, {"_id": 0})
            .sort("week_start", DESCENDING).limit(limit))
    return [{"week_start": r.get("week_start", ""), "person": r.get("person", ""),
             "items": r.get("items", []), "markdown": r.get("markdown", ""),
             "generated_at": r.get("generated_at", "")} for r in rows]


# ---------- 厂商清单 ----------

def list_vendors() -> list[str]:
    return sorted(d["_id"] for d in mongo.db().vendors.find({}, {"_id": 1}))


def add_vendor(name: str) -> None:
    try:
        mongo.db().vendors.insert_one({"_id": name, "created_at": now_iso()})
    except DuplicateKeyError:
        pass


def delete_vendor(name: str) -> None:
    mongo.db().vendors.delete_one({"_id": name})


# ---------- 自定义问题类型（后台新增，与内置 DOC_CATEGORY_ORDER 合并使用） ----------

def list_custom_categories() -> list[str]:
    return sorted(d["_id"] for d in mongo.db().custom_categories.find({}, {"_id": 1}))


def add_custom_category(name: str) -> None:
    try:
        mongo.db().custom_categories.insert_one({"_id": name, "created_at": now_iso()})
    except DuplicateKeyError:
        pass


def delete_custom_category(name: str) -> None:
    mongo.db().custom_categories.delete_one({"_id": name})


# ---------- 问题类型模板 ----------

def list_templates() -> dict[str, str]:
    return {d["_id"]: d["content"] for d in mongo.db().doc_templates.find({})}


def set_template(category: str, content: str) -> None:
    mongo.db().doc_templates.replace_one(
        {"_id": category},
        {"_id": category, "content": content, "updated_at": now_iso()},
        upsert=True,
    )


# ---------- 文档登记 ----------

def list_all() -> list[DocRecord]:
    return [_row_to_doc(d) for d in _docs().find({}).sort("uploaded_at", DESCENDING)]


def get(doc_id: str) -> DocRecord | None:
    row = _docs().find_one({"_id": doc_id})
    return _row_to_doc(row) if row else None


def find_by_hash(sha256: str, *, uploaded_by: str | None = None,
                 parent_id: str | None = None) -> DocRecord | None:
    """可按创建账号、父记录限定去重范围；空串兼容旧数据缺失字段。"""
    query: dict = {"sha256": sha256}
    for field, value in (("uploaded_by", uploaded_by), ("parent_id", parent_id)):
        if value is not None:
            query[field] = {"$in": ["", None]} if value == "" else value
    row = _docs().find_one(query)
    return _row_to_doc(row) if row else None


def count_children(doc_id: str) -> int:
    """包括处理中 / 失败的追踪，避免删除其父记录后形成孤儿。"""
    return _docs().count_documents({"parent_id": doc_id})


def delete(doc_id: str) -> bool:
    """删除文档登记，同时清掉事件状态。"""
    if count_children(doc_id):
        raise ValueError("该记录存在后续追踪，请先删除子追踪记录")
    db = mongo.db()
    db.event_status.delete_many({"doc_id": doc_id})
    return _docs().delete_one({"_id": doc_id}).deleted_count > 0


# ---------- 时间线事件的人工状态 ----------

EVENT_STATUSES = ("resolved", "unresolved", "unknown", "improving")


def get_event_status_map() -> dict[str, str]:
    """chunk_id → 人工状态。未标记的分块不在返回里，由语义判定兜底。"""
    return {d["_id"]: d["status"] for d in mongo.db().event_status.find({}, {"status": 1})}


def set_event_status(chunk_id: str, doc_id: str, status: str) -> None:
    """标记 / 清除某条时间线事件的人工状态；空串表示恢复按内容语义判定。"""
    if status:
        mongo.db().event_status.replace_one(
            {"_id": chunk_id},
            {"_id": chunk_id, "doc_id": doc_id, "status": status, "updated_at": now_iso()},
            upsert=True,
        )
    else:
        mongo.db().event_status.delete_one({"_id": chunk_id})


# 统一使用东八区时间（容器为 UTC，直接取本地时间会慢 8 小时）
CN_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
