"""企业文档访问策略；HTTP 请求显式绑定身份，内部入库任务保持受信任上下文。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json

from fastapi import HTTPException

OPERATIONS = "operations"
_INTERNAL = object()
_actor = ContextVar("knowledge_actor", default=_INTERNAL)
_trusted_history = ContextVar("knowledge_trusted_history", default=False)


def history_is_trusted() -> bool:
    return _trusted_history.get()


@contextmanager
def trusted_history_scope():
    token = _trusted_history.set(True)
    try:
        yield
    finally:
        _trusted_history.reset(token)


def is_internal() -> bool:
    return _actor.get() is _INTERNAL


def current_identity() -> dict | None:
    value = _actor.get()
    return value if isinstance(value, dict) else None


@contextmanager
def identity_scope(identity):
    token = _actor.set(identity)
    try:
        yield
    finally:
        _actor.reset(token)


def is_platform_admin() -> bool:
    actor = current_identity() or {}
    return is_internal() or (actor.get("role") == "admin" and not actor.get("public_only"))


def managed_departments(identity: dict | None = None) -> set[str]:
    actor = identity if identity is not None else current_identity() or {}
    memberships = set(actor.get("department_ids", []))
    return {key for key, role in actor.get("department_roles", {}).items()
            if key in memberships and role == "admin"}


def normalize_department_roles(departments, roles) -> dict[str, str]:
    """部门成员默认可维护文档；兼容历史 member，非法角色不授予写权限。"""
    if not isinstance(roles, dict):
        return {}
    result = {}
    for department in departments:
        role = roles.get(department, "editor")
        if role == "admin":
            result[department] = "admin"
        elif role in ("editor", "member"):
            result[department] = "editor"
    return result


def writable_departments(identity: dict | None = None) -> set[str]:
    actor = identity if identity is not None else current_identity() or {}
    memberships = set(actor.get("department_ids", []))
    return {key for key, role in actor.get("department_roles", {}).items()
            if key in memberships and role in {"admin", "editor"}}


def read_filter() -> dict:
    if is_platform_admin():
        return {}
    actor = current_identity()
    if not actor or actor.get("must_change_password"):
        return {"_id": {"$in": []}}
    if actor.get("public_only"):
        return {"visibility": "public"}
    departments = actor.get("department_ids", [])
    choices = [{"visibility": "public"}, {"department_id": {"$in": departments}},
               {"shared_department_ids": {"$in": departments}}]
    if OPERATIONS in departments:
        choices.append({"department_id": {"$in": ["", None]}})
    return {"$or": choices}


def can_read(doc) -> bool:
    if is_platform_admin():
        return True
    actor = current_identity()
    if not actor or actor.get("must_change_password"):
        return False
    if actor.get("public_only"):
        return getattr(doc, "visibility", "department") == "public"
    departments = set(actor.get("department_ids", []))
    return (getattr(doc, "visibility", "department") == "public"
            or (getattr(doc, "department_id", "") or OPERATIONS) in departments
            or bool(departments.intersection(getattr(doc, "shared_department_ids", []))))


def can_write(doc) -> bool:
    if (current_identity() or {}).get("public_only"):
        return False
    if is_platform_admin():
        return True
    return (can_read(doc) and getattr(doc, "visibility", "department") != "public"
            and (getattr(doc, "department_id", "") or OPERATIONS) in writable_departments())


def require_read(doc_id: str):
    from app.services import database
    doc = database.get(doc_id)
    if doc is None or not can_read(doc):
        raise HTTPException(status_code=404, detail="文档不存在或无权访问")
    return doc


def require_write(doc_id: str):
    doc = require_read(doc_id)
    if not can_write(doc):
        raise HTTPException(status_code=403, detail="没有此文档的修改权限")
    return doc


def creation_scope(department_id: str = "", visibility: str = "department",
                   shared_department_ids=None) -> dict:
    from app.services import database
    department_id = department_id.strip() or next(iter(sorted(writable_departments())), "") or database.default_department_id()
    shared = list(dict.fromkeys(str(value).strip() for value in shared_department_ids or [] if str(value).strip()))
    if visibility not in {"department", "public"}:
        raise HTTPException(status_code=400, detail="可见范围只能是 department / public")
    if not is_platform_admin():
        if department_id not in writable_departments():
            raise HTTPException(status_code=403, detail="没有此部门的文档维护权限")
        if visibility == "public":
            raise HTTPException(status_code=403, detail="公共文档由平台管理员发布")
        if shared and department_id not in managed_departments():
            raise HTTPException(status_code=403, detail="只有部门管理员可以授权跨部门共享")
    if not is_internal():
        existing = {row["id"] for row in database.list_departments()}
        if any(value not in existing for value in [department_id, *shared]):
            raise HTTPException(status_code=400, detail="部门不存在")
    return {"department_id": department_id, "visibility": visibility,
            "shared_department_ids": [] if visibility == "public" else [d for d in shared if d != department_id]}


def permission_stamp(identity: dict) -> str:
    from app.services import database
    payload = [identity.get("username"), identity.get("role"), identity.get("department_ids", []),
               identity.get("department_roles", {}), database.access_revision()]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def feedback_scope() -> str:
    return "" if is_internal() else "user:" + str((current_identity() or {}).get("username", "anonymous"))


def question_context(department_id: str = "") -> dict:
    """常见问题只在当前选中的部门或公共入口内共享，与个人问答历史分开。"""
    if is_internal():
        return {"scope": "internal", "department_id": "", "departments": []}
    actor = current_identity() or {}
    if actor.get("public_only"):
        return {"scope": "public", "department_id": "", "departments": []}
    if not actor or actor.get("must_change_password"):
        return {"scope": "none", "department_id": "", "departments": []}
    from app.services import database
    directory = {row["id"]: row for row in database.list_departments()}
    memberships = [key for key in actor.get("department_ids", []) if key in directory]
    options = list(directory.values()) if is_platform_admin() else [directory[key] for key in memberships]
    requested = department_id.strip()
    allowed = {row["id"] for row in options}
    if requested and requested not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该部门的常见问题")
    selected = requested or next(iter(memberships), "") or (options[0]["id"] if options else "")
    return {"scope": "department" if selected else "none", "department_id": selected, "departments": options}
