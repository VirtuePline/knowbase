"""文档登记表（MongoDB）。只存元数据，正文与分块落在文件里。

集合与原 SQLite 表的对应关系：
- documents       文档登记（_id = 文档 id）
- event_status    时间线事件人工状态（_id = chunk_id）
- vendors         厂商清单（_id = name）
- doc_templates   问题类型模板（_id = category）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import RLock
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.domain.schemas import DocRecord
from app.services import mongo

# 管理端多线程写入与部门删除共用，避免验证部门后又写回已删除的归属。
department_mutation_lock = RLock()

_INT_FIELDS = {"size", "chunks", "skipped"}
_LIST_FIELDS = {"categories", "shared_department_ids"}
_DOC_FIELDS = (
    "id", "name", "stored_name", "ext", "size", "sha256", "chunks",
    "uploaded_at", "status", "error", "summary", "skipped", "category", "categories", "vendor", "doc_type",
    "resolved", "uploaded_by", "uploaded_by_name", "parent_id", "root_id",
    "last_edited_by", "last_edited_name", "last_edited_at",
    "department_id", "visibility", "shared_department_ids",
)


def _normalize_doc_type(row: dict) -> str:
    """存量数据没有 doc_type 字段：常识库归基础文档，其余（历史工作记录）归工作记录。"""
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
    _docs().create_index([("department_id", ASCENDING), ("visibility", ASCENDING)])
    _docs().create_index("shared_department_ids")
    mongo.db().question_stats.create_index([("scope", ASCENDING), ("last_at", DESCENDING), ("count", DESCENDING)])
    initialize_enterprise()


def _row_to_doc(row: dict) -> DocRecord:
    row = dict(row)
    # 文档主键存 _id；兼容带冗余 id 字段的历史数据
    row["id"] = str(row.get("_id") or row.get("id") or "")
    row["doc_type"] = _normalize_doc_type(row)
    row["department_id"] = row.get("department_id") or "operations"
    row["visibility"] = row.get("visibility") or "department"
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
    from app.services import access
    with department_mutation_lock:
        if not access.is_internal():
            _validate_document_departments(doc.department_id, doc.shared_department_ids)
        elif doc.department_id == "operations" and not mongo.db().departments.find_one({"_id": "operations"}):
            # 命令行模型保留历史默认 ID；默认部门已删除时使用现存部门。
            doc.department_id = default_department_id()
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
    """设置录入时的解决状态（unresolved / improving / temporary / resolved）；空串清除标记。"""
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


def external_apis_enabled() -> bool:
    """缺省允许外部接口；持久化状态无法读取时拒绝出站，避免离线开关失效。"""
    try:
        row = mongo.db().app_settings.find_one({"_id": "external_apis"})
    except Exception:
        return False
    return row is None or row.get("value") == "on"


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
    from app.services import access
    scope = access.feedback_scope()
    for doc_id in {str(d).strip() for d in doc_ids or [] if str(d).strip()}:
        mongo.db().answer_penalties.update_one(
            {"_id": f"{scope}:{doc_id}" if scope else doc_id},
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
    from app.services import access
    scope = access.feedback_scope()
    prefix = scope + ":" if scope else ""
    return {d["_id"][len(prefix):]: int(d.get("count", 0))
            for d in mongo.db().answer_penalties.find({"_id": {"$in": [prefix + key for key in ids]}}, {"count": 1})}


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


# ---------- 工作周报（定时生成，按记录人汇总本周记录标题） ----------

def list_record_owners() -> list[str]:
    """有工作记录的记录人名单（去重），供周报设置勾选。"""
    try:
        names = mongo.db().documents.distinct(
            "uploaded_by_name", {"doc_type": "record", "uploaded_by_name": {"$nin": ["", None]}})
    except Exception:   # noqa: BLE001
        return []
    return sorted({str(n).strip() for n in names if str(n).strip()})


def save_weekly_report(week_start: str, person: str, items: list[dict], generated_at: str,
                       markdown: str = "", *, permission_revision: int | None = None,
                       owner_account_id: str | None = None) -> None:
    """写入 / 覆盖某人本周的周报（_id = 周起始日:记录人，天然幂等）。"""
    if owner_account_id is None:
        owner = mongo.db().users.find_one({"_id": person}, {"account_id": 1}) or {}
        owner_account_id = owner.get("account_id", "")
    mongo.db().weekly_reports.replace_one(
        {"_id": f"{week_start}:{person}"},
        {"_id": f"{week_start}:{person}", "week_start": week_start, "person": person,
         "items": items, "markdown": markdown, "generated_at": generated_at,
         "owner_account": person, "owner_account_id": owner_account_id,
         "access_revision": access_revision() if permission_revision is None else permission_revision},
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


def timeline_page(pipeline: list[dict]) -> dict:
    """时间线在数据库中筛选、统计和分页，只取回当前页及计数。"""
    return next(_docs().aggregate(pipeline, allowDiskUse=True), {})


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
    deleted = _docs().delete_one({"_id": doc_id}).deleted_count > 0
    if deleted:
        bump_access_revision()
    return deleted


# ---------- 记录附件（只挂在记录上，不进文档库、不参与索引） ----------

def add_attachments(doc_id: str, entries: list[dict]) -> None:
    if not entries:
        return
    _docs().update_one({"_id": doc_id}, {"$push": {"attachments": {"$each": entries}}})


def get_attachments(doc_id: str) -> list[dict]:
    row = _docs().find_one({"_id": doc_id}, {"attachments": 1})
    return [a for a in (row or {}).get("attachments") or [] if isinstance(a, dict)]


def remove_attachment(doc_id: str, stored: str) -> None:
    _docs().update_one({"_id": doc_id}, {"$pull": {"attachments": {"stored": stored}}})


# ---------- 时间线事件的人工状态 ----------

EVENT_STATUSES = ("resolved", "unresolved", "unknown", "improving", "temporary")


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


# ---------- 提问频率统计（最近 N 天热门问题，驱动首页快捷问题） ----------

def _question_scope(department_id: str) -> str | None:
    from app.services import access
    context = access.question_context(department_id)
    if context["scope"] == "none":
        return None
    return "department:" + context["department_id"] if context["scope"] == "department" else context["scope"]


def record_question(question: str, department_id: str = "", *, expected_revision: int | None = None) -> None:
    """同一问题在部门内累加；公共访客共用独立公共池，旧个人数据不混入。"""
    q = "".join(str(question or "").split())[:40]
    if not q:
        return
    with department_mutation_lock:
        if expected_revision is not None and access_revision() != expected_revision:
            return
        scope = _question_scope(department_id)
        if scope is None:
            return
        key = sha256((scope + "\0" + q).encode("utf-8")).hexdigest()
        update = {"$inc": {"count": 1}, "$set": {"last_at": now_iso()},
                  "$setOnInsert": {"question": q, "scope": scope}}
        collection = mongo.db().question_stats
        try:
            collection.update_one({"_id": key}, update, upsert=True)
        except DuplicateKeyError:
            collection.update_one({"_id": key}, update)


def top_questions(days: int = 7, limit: int = 4, department_id: str = "") -> list[dict]:
    """最近 days 天内提问次数最多的问题（按计数倒序）。"""
    since = (datetime.now(CN_TZ) - timedelta(days=max(1, days))).strftime("%Y-%m-%d %H:%M:%S")
    scope = _question_scope(department_id)
    if scope is None:
        return []
    query = {"last_at": {"$gte": since}, "scope": scope}
    rows = (mongo.db().question_stats.find(query, {"_id": 0})
            .sort([("count", DESCENDING), ("last_at", DESCENDING), ("question", ASCENDING)]).limit(max(1, limit)))
    return [{"question": r.get("question", ""), "count": r.get("count", 0)} for r in rows]


def initialize_enterprise() -> None:
    """幂等迁移：历史资料保留为默认部门内部资料，不自动公开。"""
    # 仅空组织首次创建内置部门，已删除的内置部门不会在重启后重新出现。
    if mongo.db().departments.find_one({}, {"_id": 1}) is None:
        mongo.db().departments.update_one({"_id": "operations"},
            {"$setOnInsert": {"name": "默认部门", "created_at": now_iso()}}, upsert=True)
    default_id = default_department_id()
    # 仅调整历史内置名称；稳定 ID、已有归属及用户自定义部门名称均保留。
    renamed = mongo.db().departments.update_one({"_id": "operations", "name": "运维部门"},
                                               {"$set": {"name": "默认部门"}})
    if renamed.modified_count:
        bump_access_revision()
    for field, value in (("department_id", default_id), ("visibility", "department"),
                         ("shared_department_ids", [])):
        _docs().update_many({field: {"$exists": False}}, {"$set": {field: value}})
    mongo.db().users.update_many({"department_ids": {"$exists": False}},
                                {"$set": {"department_ids": [default_id],
                                          "department_roles": {default_id: "editor"}}})
    # 部门仅保留管理员和编辑者；补齐旧默认角色，未知非法值原样保留且不授写。
    roles_changed = False
    for user in mongo.db().users.find({}, {"department_ids": 1, "department_roles": 1, "account_id": 1}):
        if not user.get("account_id"):
            mongo.db().users.update_one({"_id": user["_id"], "account_id": user.get("account_id")},
                                        {"$set": {"account_id": uuid4().hex}})
        roles = user.get("department_roles", {})
        if not isinstance(roles, dict):
            continue
        updated = dict(roles)
        for department in user.get("department_ids", []):
            if department not in roles or roles[department] == "member":
                updated[department] = "editor"
        if updated != roles:
            # 账号权限可能同时被管理员修改，只更新我们实际读取到的版本。
            result = mongo.db().users.update_one(
                {"_id": user["_id"], "department_roles": user.get("department_roles"),
                 "department_ids": user.get("department_ids", [])},
                {"$set": {"department_roles": updated}},
            )
            roles_changed = roles_changed or bool(result.modified_count)
    if roles_changed:
        bump_access_revision()


def list_departments() -> list[dict]:
    rows = [{"id": row["_id"], "name": row.get("name", row["_id"])}
            for row in mongo.db().departments.find({}).sort("name", ASCENDING)]
    return rows or [{"id": "operations", "name": "默认部门"}]


def default_department_id() -> str:
    """内部录入或历史迁移的缺省归属，优先保留仍存在的历史默认部门。"""
    departments = list_departments()
    return next((row["id"] for row in departments if row["id"] == "operations"), departments[0]["id"])


def department_names(department_ids) -> dict[str, str]:
    """仅查询调用方已获授权文档涉及的部门，不向公共端暴露完整组织目录。"""
    ids = set(department_ids)
    if not ids:
        return {}
    names = {row["_id"]: row.get("name") or "未命名部门"
             for row in mongo.db().departments.find({"_id": {"$in": list(ids)}}, {"name": 1})}
    return {key: names.get(key, "默认部门" if key == "operations" else "未命名部门") for key in ids}


def save_department(department_id: str, name: str, *, create: bool | None = None) -> dict:
    import re
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", department_id):
        raise ValueError("部门编号须为小写字母开头的字母、数字、下划线或短横线")
    name = name.strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("部门名称须为1–60个字符")
    with department_mutation_lock:
        departments = mongo.db().departments
        if create is True:
            departments.insert_one({"_id": department_id, "name": name, "created_at": now_iso()})
        elif create is False:
            if not departments.update_one({"_id": department_id}, {"$set": {"name": name}}).matched_count:
                raise DepartmentNotFound(department_id)
        else:
            # 内部初始化与维护脚本保留幂等写入，HTTP 创建/重命名严格区分。
            departments.update_one({"_id": department_id},
                {"$set": {"name": name}, "$setOnInsert": {"created_at": now_iso()}}, upsert=True)
        bump_access_revision()
    return {"id": department_id, "name": name}


class DepartmentNotFound(LookupError):
    pass


class DepartmentDeleteConflict(ValueError):
    pass


def _department_owner_query(department_id: str) -> dict:
    # 兼容尚未运行启动迁移的历史文档；空归属只属于当前缺省部门。
    values = [department_id, "", None] if department_id == default_department_id() else [department_id]
    return {"department_id": {"$in": values}}


def _department_member_query(department_id: str) -> dict:
    choices = [{"department_ids": department_id}, {f"department_roles.{department_id}": {"$exists": True}}]
    if department_id == default_department_id():
        choices.append({"department_ids": {"$exists": False}})
    return {"$or": choices}


def department_deletion_info(department_id: str) -> dict:
    department = mongo.db().departments.find_one({"_id": department_id})
    if department is None:
        raise DepartmentNotFound(department_id)
    return {
        "department": {"id": department_id, "name": department.get("name") or "未命名部门"},
        "documents": _docs().count_documents(_department_owner_query(department_id)),
        "members": mongo.db().users.count_documents(_department_member_query(department_id)),
        "shared_documents": _docs().count_documents({"shared_department_ids": department_id}),
        "departments": [row for row in list_departments() if row["id"] != department_id],
    }


def delete_department(department_id: str, target_department_id: str = "") -> dict:
    """转移资料并移除部门关联；不删除任何企业账号、正文或附件。"""
    with department_mutation_lock:
        info = department_deletion_info(department_id)
        targets = {row["id"] for row in info["departments"]}
        if not targets:
            raise DepartmentDeleteConflict("至少保留一个部门，请先创建其他部门")
        target = target_department_id.strip()
        if target and target not in targets:
            raise ValueError("迁移目标必须是其他现存部门")
        if info["documents"] and not target:
            raise DepartmentDeleteConflict("该部门仍有文档，请选择迁移目标部门")
        default_id = default_department_id()
        # 起止均刷新权限版本：并发问答与旧周报不能沿用迁移过程中的上下文。
        bump_access_revision()
        moved = 0
        if info["documents"]:
            moved = _docs().update_many(_department_owner_query(department_id), {
                "$set": {"department_id": target},
                "$pull": {"shared_department_ids": {"$in": [department_id, target]}},
            }).modified_count
        _docs().update_many({"shared_department_ids": department_id},
                            {"$pull": {"shared_department_ids": department_id}})
        users = mongo.db().users
        removed = users.update_many(_department_member_query(department_id), {
            "$pull": {"department_ids": department_id},
            "$unset": {f"department_roles.{department_id}": ""},
        }).modified_count
        if department_id == default_id:
            # 显式空归属阻止旧账号在下次启动时被重新分配给另一个部门。
            users.update_many({"department_ids": {"$exists": False}},
                              {"$set": {"department_ids": [], "department_roles": {}}})
        mongo.db().departments.delete_one({"_id": department_id})
        mongo.db().question_stats.delete_many({"scope": "department:" + department_id})
        bump_access_revision()
        return {"deleted": True, "department_id": department_id, "target_department_id": target,
                "moved_documents": moved, "removed_memberships": removed,
                "revoked_shared_documents": info["shared_documents"]}


def _validate_document_departments(department_id: str, shared_department_ids: list[str]) -> None:
    from fastapi import HTTPException
    existing = {row["id"] for row in list_departments()}
    if any(value not in existing for value in [department_id, *shared_department_ids]):
        raise HTTPException(status_code=409, detail="部门已变更，请刷新页面后重试")


def set_document_access(doc_id: str, department_id: str, visibility: str,
                        shared_department_ids: list[str]) -> bool:
    with department_mutation_lock:
        _validate_document_departments(department_id, shared_department_ids)
        result = _docs().update_one({"_id": doc_id}, {"$set": {
            "department_id": department_id, "visibility": visibility,
            "shared_department_ids": list(shared_department_ids),
        }})
        if result.matched_count:
            bump_access_revision()
    return bool(result.matched_count)


def bump_access_revision() -> None:
    mongo.db().app_settings.update_one({"_id": "access_revision"}, {"$inc": {"version": 1}}, upsert=True)


def access_revision() -> int:
    try:
        row = mongo.db().app_settings.find_one({"_id": "access_revision"})
    except AttributeError:  # 不带设置集合的旧内存测试替身
        return 0
    return int((row or {}).get("version", 0))
