"""统一入口：访客只读公共资料，登录后按账号权限使用完整工作台。

`python -m app.cli serve` 或 `uvicorn app.main:app` 只启动一个服务。
search_app / admin_app 保留给旧部署与隔离权限的回归测试，不再同时启动。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import re

from fastapi import Cookie, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.accounts import auth_router, identity_payload, require_login
from app.api.routes import admin_router, search_router
from app.config import settings
from app.services import audit, auth, database, access, mongo
from app.services.retrieval_service import retrieval


@asynccontextmanager
async def _lifespan(app: FastAPI):
    database.init_db()
    _migrate_images()
    # 访客与账号共用入口，首次启动准备初始企业账号。
    auth.ensure_admin_user(settings.auth_admin_user, settings.auth_admin_password)
    if app.state.portal in {"admin", "unified"}:
        # 周报定时生成：守护线程每分钟检查计划时间（幂等，同一周重复到点不会重复生成）
        from app.services import weekly
        weekly.start_weekly_scheduler()
    retrieval.load()
    # 预热检索：把嵌入模型首次连接 / 首次推理的开销留在启动阶段，
    # 否则进程启动后的第一个提问要多等约 2 秒
    try:
        retrieval.search("预热", top_k=1, use_rerank=False)
    except Exception:   # noqa: BLE001 预热失败不影响服务
        pass
    yield


def _migrate_images():
    """给历史正文图片绑定所属文档；不会将未引用的旧图片自动公开。"""
    for row in database._docs().find({}, {"stored_name": 1, "uploaded_by": 1}):
        path = (settings.raw_dir / row.get("stored_name", "")).resolve()
        if not path.is_relative_to(settings.raw_dir.resolve()) or path.suffix.lower() not in {".md", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for name in set(re.findall(r"/images/([A-Za-z0-9_.-]+)", text)):
            mongo.db().image_assets.update_one({"_id": name}, {
                "$addToSet": {"doc_ids": str(row["_id"])},
                "$setOnInsert": {"uploaded_by": row.get("uploaded_by", "")}}, upsert=True)


class AssetFiles(StaticFiles):
    """仅公开前端样式与脚本，HTML 必须通过各自的页面权限入口。"""

    async def get_response(self, path: str, scope):
        if Path(path).suffix.lower() not in {".css", ".js"}:
            raise HTTPException(status_code=404, detail="资源不存在")
        return await super().get_response(path, scope)


def require_read_access(admin_session: str | None = Cookie(default=None)) -> dict | None:
    """允许公共访客读取；已有账号会话仍须通过首次改密检查。"""
    identity = access.current_identity()
    if identity and not identity.get("public_only"):
        return require_login(admin_session)
    return identity


def _make_app(title_suffix: str, routers: list, mode: str) -> FastAPI:
    app = FastAPI(
        title=f"企业知识库 — {title_suffix}",
        description="公共知识、部门资料与受权限控制的企业问答服务",
        version="0.2.0",
        lifespan=_lifespan,
    )
    app.state.portal = mode
    for router in routers:
        dependencies = []
        if router is search_router:
            if mode == "admin":
                dependencies = [Depends(require_login)]
            elif mode == "unified":
                dependencies = [Depends(require_read_access)]
        app.include_router(router, dependencies=dependencies)

    @app.middleware("http")
    async def bind_access(request, call_next):
        token = request.cookies.get(auth.SESSION_COOKIE)
        new_public_token = None
        identity = auth.session_identity(token) if token and mode != "search" else None
        public_access = mode == "search" or (mode == "unified" and identity is None)
        public_token = request.cookies.get(auth.PUBLIC_COOKIE)
        if public_access:
            identity = auth.public_identity(public_token)
            if identity is None:
                # 公共只读资源无需创建会话；仅页面初始化与问答需要浏览器上下文令牌。
                if request.url.path in {"/", "/api/mode", "/api/chat"}:
                    new_public_token = public_token = auth.create_public_session()
                    identity = auth.public_identity(public_token)
                else:
                    identity = {"username": "", "role": "guest", "public_only": True,
                                "department_ids": [], "department_roles": {}}
        stamp = access.permission_stamp(identity) if identity and request.url.path == "/api/chat" else None
        with access.identity_scope(identity):
            response = await call_next(request)
            if stamp and response.status_code == 200:
                refreshed = auth.public_identity(public_token) if public_access else auth.session_identity(token)
                if not refreshed or access.permission_stamp(refreshed) != stamp:
                    return JSONResponse({"detail": "访问权限已更新，请重新提问"}, status_code=409,
                                        headers={"Cache-Control": "no-store"})
            if request.url.path.startswith(("/api/", "/images/")):
                response.headers["Cache-Control"] = "no-store"
            if new_public_token:
                response.set_cookie(auth.PUBLIC_COOKIE, new_public_token, max_age=auth.SESSION_TTL,
                                    httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/")
            return response

    if mode in {"admin", "unified"}:
        # 管理端审计：每次写操作（非 GET）记录 IP、时间、动作、账号到 MongoDB
        @app.middleware("http")
        async def audit_middleware(request, call_next):
            body = b""
            actor = audit.session_user(request.cookies.get(auth.SESSION_COOKIE))
            if request.method != "GET" and "application/json" in request.headers.get("content-type", ""):
                body = await request.body()   # 先读后放行；starlette 缓存 body，不影响路由解析
            response = await call_next(request)
            if request.method != "GET" and request.url.path.startswith("/api"):
                content_type = request.headers.get("content-type", "")
                detail = (audit.auth_detail_from_body(body, content_type)
                          if request.url.path.startswith("/api/admin/")
                          else audit.detail_from_body(body, content_type))
                audit.record(
                    ip=audit.client_ip(request),
                    method=request.method,
                    path=request.url.path,
                    status=response.status_code,
                    detail=detail,
                    user=actor,
                )
            return response

    @app.get("/api/mode")
    def current_mode(admin_session: str | None = Cookie(default=None)) -> dict:
        identity = access.current_identity() or {}
        if mode == "search" or (mode == "unified" and identity.get("public_only")):
            return {"mode": "search", "portal": "search", "public_access": True, "auth": False,
                    "authenticated": False, "admin": False, "user": "", "account": "", "username": "访客",
                    "display_name": "公共访问", "role": "guest", "can_write": False, "can_manage_users": False,
                    "can_register": False, "department_ids": [], "department_roles": {}, "departments": [],
                    "must_change_password": False, "permission_stamp": access.permission_stamp(identity),
                    "external_apis_enabled": database.external_apis_enabled()}
        return {"mode": "admin", "portal": "admin", **identity_payload(auth.session_identity(admin_session)),
                "external_apis_enabled": database.external_apis_enabled()}

    def readable_identity(admin_session: str | None = Cookie(default=None)):
        if mode == "search":
            return access.current_identity()
        return require_read_access(admin_session) if mode == "unified" else require_login(admin_session)

    @app.get("/images/{name}")
    def image(name: str, identity: dict = Depends(readable_identity)):
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|gif|webp|bmp)", name, re.I):
            raise HTTPException(status_code=404, detail="图片不存在")
        asset = mongo.db().image_assets.find_one({"_id": name})
        authenticated = not identity.get("public_only") and mode != "search"
        allowed = identity["role"] == "admin" and authenticated
        if asset and not allowed:
            linked = asset.get("doc_ids", [])
            allowed = (authenticated and not linked and asset.get("uploaded_by") == identity["username"]) or any(
                doc is not None and access.can_read(doc) for doc in (database.get(key) for key in linked))
        path = settings.images_dir / name
        if not allowed or not path.is_file():
            raise HTTPException(status_code=404, detail="图片不存在或无权访问")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    if settings.web_dir.is_dir():
        app.mount("/static", AssetFiles(directory=str(settings.web_dir)), name="static")

        @app.get("/")
        def index(admin_session: str | None = Cookie(default=None)):          # noqa: F811
            if mode == "search":
                return FileResponse(settings.web_dir / "index.html")
            identity = auth.session_identity(admin_session)
            if identity is None:
                if mode == "unified":
                    return FileResponse(settings.web_dir / "index.html")
                return RedirectResponse("/login", status_code=303)
            if identity.get("must_change_password", False):
                return RedirectResponse("/change-password", status_code=303)
            return FileResponse(settings.web_dir / "index.html")

        @app.get("/login")
        def login_page(admin_session: str | None = Cookie(default=None)):
            if mode == "search":
                return RedirectResponse("/", status_code=303)
            identity = auth.session_identity(admin_session)
            if identity is not None:
                target = "/change-password" if identity.get("must_change_password", False) else "/"
                return RedirectResponse(target, status_code=303)
            return FileResponse(settings.web_dir / "login.html")

        @app.get("/change-password")
        def change_password_page(admin_session: str | None = Cookie(default=None)):
            if mode == "search":
                return RedirectResponse("/", status_code=303)
            if auth.session_identity(admin_session) is None:
                return RedirectResponse("/login", status_code=303)
            return FileResponse(settings.web_dir / "change-password.html")

        @app.get("/register")
        def register_page(admin_session: str | None = Cookie(default=None)):
            if mode == "search":
                return RedirectResponse("/", status_code=303)
            identity = auth.session_identity(admin_session)
            if identity is None:
                return RedirectResponse("/login", status_code=303)
            if identity.get("must_change_password", False):
                return RedirectResponse("/change-password", status_code=303)
            if identity["role"] != "admin" and not access.managed_departments(identity):
                raise HTTPException(status_code=403, detail="只有管理员可以注册账号")
            return FileResponse(settings.web_dir / "register.html")
    return app


# 显式使用旧应用名时保留原有权限行为。
search_app = _make_app("公共查询端", [search_router], "search")

admin_app = _make_app("管理端", [search_router, auth_router, admin_router], "admin")

# 单端口按登录状态提供公共查询或账号工作台，写操作仍由路由校验登录与角色。
app = _make_app("知识工作台", [search_router, auth_router, admin_router], "unified")
