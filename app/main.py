"""应用入口：两个 FastAPI 实例，查询端与管理端分别挂载。

    查询端（search_app）：问答 + 只读文档接口（列表、预览、下载）
    管理端（admin_app）：查询端全部功能 + 上传、替换、编辑、删除、重建

`python -m app.cli serve` 同时启动两端（端口见 config.yaml 的 server 段）；
也可单独运行 uvicorn app.main:search_app / app.main:admin_app。
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
    # 单独部署查询端也需要可用的初始企业账号。
    auth.ensure_admin_user(settings.auth_admin_user, settings.auth_admin_password)
    if app.state.portal == "admin":
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


def _make_app(title_suffix: str, routers: list, mode: str) -> FastAPI:
    app = FastAPI(
        title=f"企业知识库 — {title_suffix}",
        description="公共知识、部门资料与受权限控制的企业问答服务",
        version="0.2.0",
        lifespan=_lifespan,
    )
    app.state.portal = mode
    for router in routers:
        dependencies = [Depends(require_login)] if router is search_router else []
        app.include_router(router, dependencies=dependencies)

    @app.middleware("http")
    async def bind_access(request, call_next):
        token = request.cookies.get(auth.SESSION_COOKIE)
        identity = auth.session_identity(token) if token else None
        stamp = access.permission_stamp(identity) if identity and request.url.path == "/api/chat" else None
        with access.identity_scope(identity):
            response = await call_next(request)
            if stamp and response.status_code == 200:
                refreshed = auth.session_identity(token)
                if not refreshed or access.permission_stamp(refreshed) != stamp:
                    return JSONResponse({"detail": "访问权限已更新，请重新提问"}, status_code=409,
                                        headers={"Cache-Control": "no-store"})
            if request.url.path.startswith(("/api/", "/images/")):
                response.headers["Cache-Control"] = "no-store"
            return response

    if mode == "admin":
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
        return {"mode": mode, "portal": mode, **identity_payload(auth.session_identity(admin_session))}

    @app.get("/images/{name}")
    def image(name: str, identity: dict = Depends(require_login)):
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|gif|webp|bmp)", name, re.I):
            raise HTTPException(status_code=404, detail="图片不存在")
        asset = mongo.db().image_assets.find_one({"_id": name})
        allowed = identity["role"] == "admin"
        if asset and not allowed:
            linked = asset.get("doc_ids", [])
            allowed = (not linked and asset.get("uploaded_by") == identity["username"]) or any(
                doc is not None and access.can_read(doc) for doc in (database.get(key) for key in linked))
        path = settings.images_dir / name
        if not allowed or not path.is_file():
            raise HTTPException(status_code=404, detail="图片不存在或无权访问")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    if settings.web_dir.is_dir():
        app.mount("/static", AssetFiles(directory=str(settings.web_dir)), name="static")

        @app.get("/")
        def index(admin_session: str | None = Cookie(default=None)):          # noqa: F811
            identity = auth.session_identity(admin_session)
            if identity is None:
                return RedirectResponse("/login", status_code=303)
            if identity.get("must_change_password", False):
                return RedirectResponse("/change-password", status_code=303)
            return FileResponse(settings.web_dir / "index.html")

        @app.get("/login")
        def login_page(admin_session: str | None = Cookie(default=None)):
            identity = auth.session_identity(admin_session)
            if identity is not None:
                target = "/change-password" if identity.get("must_change_password", False) else "/"
                return RedirectResponse(target, status_code=303)
            return FileResponse(settings.web_dir / "login.html")

        @app.get("/change-password")
        def change_password_page(admin_session: str | None = Cookie(default=None)):
            if auth.session_identity(admin_session) is None:
                return RedirectResponse("/login", status_code=303)
            return FileResponse(settings.web_dir / "change-password.html")

        @app.get("/register")
        def register_page(admin_session: str | None = Cookie(default=None)):
            identity = auth.session_identity(admin_session)
            if identity is None:
                return RedirectResponse("/login", status_code=303)
            if identity.get("must_change_password", False):
                return RedirectResponse("/change-password", status_code=303)
            if identity["role"] != "admin" and not access.managed_departments(identity):
                raise HTTPException(status_code=403, detail="只有管理员可以注册账号")
            return FileResponse(settings.web_dir / "register.html")
    return app


# 查询端：登录后的只读知识访问，账号接口仍按角色授权。
search_app = _make_app("查询端", [search_router, auth_router], "search")

# 管理端：查询端 + 登录 + 写操作（写操作需登录）。仅对运维人员开放，不挂在外网。
admin_app = _make_app("管理端", [search_router, auth_router, admin_router], "admin")

# 兼容旧的 `uvicorn app.main:app` 启动方式：指向只读的查询端，避免旧脚本意外暴露写接口
app = search_app
