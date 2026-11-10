"""管理端账号接口：所有账号须登录，管理员可注册普通账号。"""
from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.services import auth

auth_router = APIRouter(prefix="/api", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=128)


class RegisterRequest(BaseModel):
    account: str = Field(default="", max_length=128)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(default="", max_length=128)
    role: str = "user"


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(default="", max_length=128)
    new_password: str = Field(default="", max_length=128)
    confirm_password: str = Field(default="", max_length=128)


def identity_payload(identity: dict | None) -> dict:
    """保留 admin 代表管理端已登录的旧前端契约，角色另由 role 表达。"""
    return {"admin": bool(identity), "authenticated": bool(identity), "auth": True,
            "user": identity["username"] if identity else "",
            "account": identity["username"] if identity else "",
            "username": identity.get("display_name", identity["username"]) if identity else "",
            "display_name": identity.get("display_name", identity["username"]) if identity else "",
            "role": identity["role"] if identity else "",
            "must_change_password": bool(identity and identity.get("must_change_password")),
            "can_register": bool(identity and identity["role"] == "admin" and not identity.get("must_change_password"))}


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
    if identity["role"] != "admin":
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
        auth.create_user(account, role="user", display_name=display_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="账号已存在，请使用其他账号") from None
    return {"registered": True, "user": account, "account": account, "username": display_name,
            "display_name": display_name, "role": "user", "must_change_password": True}


@auth_router.get("/admin/users")
def list_all_users(identity: dict = Depends(require_account_admin)) -> dict:
    return {"users": auth.list_users()}


@auth_router.delete("/admin/users/{account}")
def delete_user(account: str, identity: dict = Depends(require_account_admin)) -> dict:
    try:
        auth.delete_user(account.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"deleted": True, "user": account}
