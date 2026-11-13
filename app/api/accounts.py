"""管理端账号接口：所有账号须登录，管理员可注册普通账号。"""
from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.services import auth, access, database, mongo

auth_router = APIRouter(prefix="/api", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=128)


class RegisterRequest(BaseModel):
    account: str = Field(default="", max_length=128)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(default="", max_length=128)
    role: str = "user"
    department_ids: list[str] = Field(default_factory=lambda: [database.default_department_id()])
    department_roles: dict[str, str] = Field(default_factory=dict)


class MembershipRequest(BaseModel):
    department_ids: list[str]
    department_roles: dict[str, str] = Field(default_factory=dict)


class DepartmentRequest(BaseModel):
    id: str = ""
    name: str


class DepartmentDeleteRequest(BaseModel):
    target_department_id: str = Field(default="", max_length=48)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(default="", max_length=128)
    new_password: str = Field(default="", max_length=128)
    confirm_password: str = Field(default="", max_length=128)


def identity_payload(identity: dict | None) -> dict:
    """保留 admin 代表管理端已登录的旧前端契约，角色另由 role 表达。"""
    try:
        departments = database.list_departments() if identity else []
    except AttributeError:
        departments = [{"id": "operations", "name": "默认部门"}]
    manager = bool(identity and (identity["role"] == "admin" or access.managed_departments(identity)))
    return {"admin": bool(identity), "authenticated": bool(identity), "auth": True,
            "user": identity["username"] if identity else "",
            "account": identity["username"] if identity else "",
            "username": identity.get("display_name", identity["username"]) if identity else "",
            "display_name": identity.get("display_name", identity["username"]) if identity else "",
            "role": identity["role"] if identity else "",
            "must_change_password": bool(identity and identity.get("must_change_password")),
            "can_register": manager and not identity.get("must_change_password", False) if identity else False,
            "department_ids": identity.get("department_ids", []) if identity else [],
            "department_roles": identity.get("department_roles", {}) if identity else {},
            "departments": departments,
            "can_manage_users": manager,
            "can_write": bool(identity and (identity["role"] == "admin" or access.writable_departments(identity))),
            "permission_stamp": access.permission_stamp(identity) if identity else ""}


def require_session(admin_session: str | None = Cookie(default=None)) -> dict:
    identity = auth.session_identity(admin_session)
    if identity is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录管理端")
    return identity


def require_login(admin_session: str | None = Cookie(default=None)) -> dict:
    identity = require_session(admin_session)
    if identity["must_change_password"]:
        raise HTTPException(status_code=403, detail="请先设置登录密码")
    return identity


def require_account_admin(identity: dict = Depends(require_login)) -> dict:
    if identity["role"] != "admin" and not access.managed_departments(identity):
        raise HTTPException(status_code=403, detail="只有管理员可以注册账号")
    return identity


@auth_router.post("/admin/login")
def login(req: LoginRequest, response: Response, request: Request) -> dict:
    username = req.username.strip()
    identity = auth.authenticate_identity(username, req.password) if username else None
    if identity is None:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    # 登录成功时撤销同一浏览器已有会话，防止积累可复用旧 token。
    auth.revoke(request.cookies.get(auth.SESSION_COOKIE))
    token = auth.create_session(identity["username"], password_version=identity["password_version"])
    identity = auth.session_identity(token)
    if identity is None:
        raise HTTPException(status_code=401, detail="账号不可用，请重新登录")
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL,
                        httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/")
    return identity_payload(identity)


@auth_router.post("/admin/logout")
def logout(response: Response, admin_session: str | None = Cookie(default=None)) -> dict:
    auth.revoke(admin_session)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return identity_payload(None)


@auth_router.get("/admin/session")
def session_state(admin_session: str | None = Cookie(default=None)) -> dict:
    return identity_payload(auth.session_identity(admin_session))


@auth_router.post("/admin/change-password")
def change_password(req: ChangePasswordRequest, response: Response, request: Request,
                    identity: dict = Depends(require_session)) -> dict:
    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    try:
        updated = auth.change_password(identity["username"], req.current_password, req.new_password,
                                       expected_version=identity["password_version"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except auth.PasswordChangeConflict:
        raise HTTPException(status_code=409, detail="密码已被更新，请重新登录") from None
    token = auth.create_session(identity["username"], password_version=updated["password_version"])
    if auth.session_identity(token) is None:
        raise HTTPException(status_code=401, detail="账号状态已变更，请重新登录")
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL,
                        httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/")
    return identity_payload(updated)


@auth_router.post("/admin/register", status_code=201)
def register(req: RegisterRequest, identity: dict = Depends(require_account_admin)) -> dict:
    if req.role != "user":
        raise HTTPException(status_code=400, detail="注册账号的角色只能是普通用户")
    try:
        account, display_name = auth.validate_registration(req.account, req.username, req.password)
        with database.department_mutation_lock:
            departments, roles = _validate_memberships(req.department_ids, req.department_roles, identity)
            auth.create_user(account, role="user", display_name=display_name,
                             department_ids=departments, department_roles=roles)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="账号已存在，请使用其他账号") from None
    return {"registered": True, "user": account, "account": account, "username": display_name,
            "display_name": display_name, "role": "user", "must_change_password": True}


@auth_router.get("/admin/users")
def list_all_users(identity: dict = Depends(require_account_admin)) -> dict:
    users = auth.list_users()
    if identity["role"] != "admin":
        managed = access.managed_departments(identity)
        users = [user for user in users if user["role"] != "admin" and managed.intersection(user["department_ids"])]
        users = [{**user, "department_ids": [d for d in user["department_ids"] if d in managed],
                  "department_roles": {d: r for d, r in user["department_roles"].items() if d in managed}}
                 for user in users]
    return {"users": users}


@auth_router.delete("/admin/users/{account}")
def delete_user(account: str, identity: dict = Depends(require_account_admin)) -> dict:
    if identity["role"] != "admin":
        raise HTTPException(status_code=403, detail="删除企业账号仅限平台管理员；部门管理员可移除本部门成员")
    try:
        auth.delete_user(account.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"deleted": True, "user": account}


def _validate_memberships(departments, roles, identity):
    departments = list(dict.fromkeys(departments))
    existing = {row["id"] for row in database.list_departments()}
    if any(d not in existing for d in departments) or set(roles) - set(departments):
        raise ValueError("部门不存在或角色与成员部门不匹配")
    if any(role not in {"admin", "editor"} for role in roles.values()):
        raise ValueError("部门角色只能是 admin / editor")
    if identity["role"] != "admin" and set(departments) - access.managed_departments(identity):
        raise HTTPException(status_code=403, detail="只能管理本部门成员")
    return departments, {d: roles.get(d, "editor") for d in departments}


@auth_router.patch("/admin/users/{account}/departments")
def set_memberships(account: str, req: MembershipRequest, identity: dict = Depends(require_account_admin)):
    with database.department_mutation_lock:
        return _set_memberships(account, req, identity)


def _set_memberships(account: str, req: MembershipRequest, identity: dict):
    row = mongo.db().users.find_one({"_id": account})
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    if row.get("role") == "admin" and identity["role"] != "admin":
        raise HTTPException(status_code=403, detail="不能修改平台管理员")
    try:
        departments, roles = _validate_memberships(req.department_ids, req.department_roles, identity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if identity["role"] != "admin":
        managed = access.managed_departments(identity)
        original = auth._row_identity(row)
        departments += [d for d in original["department_ids"] if d not in managed]
        stored_roles = row.get("department_roles")
        for department in original["department_ids"]:
            if department not in managed:
                # 非法旧角色必须保持拒绝写入；不能因清理掉角色键而变为默认编辑者。
                roles[department] = original["department_roles"].get(
                    department, stored_roles.get(department) if isinstance(stored_roles, dict) else None)
    mongo.db().users.update_one({"_id": account}, {"$set": {
        "department_ids": departments, "department_roles": roles}})
    database.bump_access_revision()
    visible = set(departments) if identity["role"] == "admin" else access.managed_departments(identity)
    return {"updated": True, "account": account, "department_ids": [d for d in departments if d in visible],
            "department_roles": {d: r for d, r in roles.items() if d in visible}}


@auth_router.get("/departments")
def departments(identity: dict = Depends(require_login)):
    return {"departments": database.list_departments()}


def _save_department(department_id, name, identity, *, create):
    if identity["role"] != "admin":
        raise HTTPException(status_code=403, detail="只有平台管理员可以维护部门")
    try:
        department = database.save_department(department_id, name, create=create)
        return {"department": department, "departments": database.list_departments()}
    except database.DepartmentNotFound:
        raise HTTPException(status_code=404, detail="部门不存在") from None
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="部门标识已存在，请使用其他标识") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@auth_router.post("/departments")
def create_department(req: DepartmentRequest, identity: dict = Depends(require_login)):
    return _save_department(req.id, req.name, identity, create=True)


@auth_router.put("/departments/{department_id}")
def rename_department(department_id: str, req: DepartmentRequest, identity: dict = Depends(require_login)):
    return _save_department(department_id, req.name, identity, create=False)


def _require_platform_admin(identity: dict) -> None:
    if identity["role"] != "admin":
        raise HTTPException(status_code=403, detail="只有平台管理员可以删除部门")


@auth_router.get("/departments/{department_id}/deletion")
def department_deletion_info(department_id: str, identity: dict = Depends(require_login)):
    _require_platform_admin(identity)
    try:
        return database.department_deletion_info(department_id)
    except database.DepartmentNotFound:
        raise HTTPException(status_code=404, detail="部门不存在") from None


@auth_router.delete("/departments/{department_id}")
def delete_department(department_id: str, req: DepartmentDeleteRequest | None = None,
                      identity: dict = Depends(require_login)):
    _require_platform_admin(identity)
    try:
        return database.delete_department(department_id, req.target_department_id if req else "")
    except database.DepartmentNotFound:
        raise HTTPException(status_code=404, detail="部门不存在") from None
    except database.DepartmentDeleteConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
