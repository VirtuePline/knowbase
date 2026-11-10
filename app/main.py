"""应用入口：两个 FastAPI 实例，查询端与管理端分别挂载。

    查询端（search_app）：问答 + 只读文档接口（列表、预览、下载）
    管理端（admin_app）：查询端全部功能 + 上传、替换、编辑、删除、重建

`python -m app.cli serve` 同时启动两端（端口见 config.yaml 的 server 段）；
也可单独运行 uvicorn app.main:search_app / app.main:admin_app。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.accounts import auth_router, identity_payload, require_login
from app.api.routes import admin_router, search_router
from app.config import settings
from app.services import audit, auth, database
from app.services.retrieval_service import retrieval


@asynccontextmanager
async def _lifespan(app: FastAPI):
    database.init_db()
    if app.state.portal == "admin":
        # 管理端强制认证，旧配置 auth.enabled=false 也不能跳过初始化与登录。
        auth.ensure_admin_user(settings.auth_admin_user, settings.auth_admin_password)
    retrieval.load()
    # 预热检索：把嵌入模型首次连接 / 首次推理的开销留在启动阶段，
    # 否则进程启动后的第一个提问要多等约 2 秒
    try:
        retrieval.search("预热", top_k=1, use_rerank=False)
    except Exception:   # noqa: BLE001 预热失败不影响服务
        pass
    yield


class AssetFiles(StaticFiles):
    """仅公开前端样式与脚本，HTML 必须通过各自的页面权限入口。"""

    async def get_response(self, path: str, scope):
        if Path(path).suffix.lower() not in {".css", ".js"}:
            raise HTTPException(status_code=404, detail="资源不存在")
        return await super().get_response(path, scope)


def _make_app(title_suffix: str, routers: list, mode: str) -> FastAPI:
    app = FastAPI(
        title=f"工厂维保 / MES 运维知识库助手 — {title_suffix}",
        description="基于本地文档的维保报修与 MES 运维问答服务",
        version="0.2.0",
        lifespan=_lifespan,
    )
    app.state.portal = mode
    for router in routers:
        dependencies = [Depends(require_login)] if mode == "admin" and router is search_router else []
        app.include_router(router, dependencies=dependencies)

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
        if mode == "admin":
            return {"mode": mode, "portal": mode, **identity_payload(auth.session_identity(admin_session))}
        return {"mode": mode, "portal": mode, "admin": False, "authenticated": False,
                "auth": False, "user": "", "role": "", "can_register": False,
                "must_change_password": False}

    if settings.images_dir.is_dir():
        app.mount("/images", StaticFiles(directory=str(settings.images_dir)), name="images")

    if settings.web_dir.is_dir():
        app.mount("/static", AssetFiles(directory=str(settings.web_dir)), name="static")

        @app.get("/")
        def index(admin_session: str | None = Cookie(default=None)):          # noqa: F811
            if mode == "admin":
                identity = auth.session_identity(admin_session)
                if identity is None:
                    return RedirectResponse("/login", status_code=303)
                if identity.get("must_change_password", False):
                    return RedirectResponse("/change-password", status_code=303)
            return FileResponse(settings.web_dir / "index.html")

        if mode == "admin":
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
                if identity["role"] != "admin":
                    raise HTTPException(status_code=403, detail="只有管理员可以注册账号")
                return FileResponse(settings.web_dir / "register.html")
    return app


# 查询端：只读接口。可直接暴露给车间终端。
search_app = _make_app("查询端", [search_router], "search")

# 管理端：查询端 + 登录 + 写操作（写操作需登录）。仅对运维人员开放，不挂在外网。
admin_app = _make_app("管理端", [search_router, auth_router, admin_router], "admin")

# 兼容旧的 `uvicorn app.main:app` 启动方式：指向只读的查询端，避免旧脚本意外暴露写接口
app = search_app
