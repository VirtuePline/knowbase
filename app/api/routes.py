"""HTTP 接口。

search_router — 只读：问答、文档列表、预览、下载。查询端与管理端都挂载。
admin_router  — 读写：上传、替换、编辑、删除、重建。只挂在管理端，查询端请求这些地址会得到 404 / 405。
"""
from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api.accounts import auth_router, require_login
from app.core.answer import service as answer_service
from app.core.llm import client as llm
from app.config import settings
from app.domain import taxonomy
from app.services import audit, database, kb, recordmeta
from app.services import timeline as timeline_service
from app.services import weekly as weekly_service

# ---------- Search（只读）----------
search_router = APIRouter(prefix="/api", tags=["search"])


class ChatRequest(BaseModel):
    question: str = Field(default="", description="工程师提问")
    history: list[dict] = Field(default_factory=list, description="预留的上下文轮次")
    fast_mode: bool | None = Field(default=None, description="本次是否快速问答；缺省用服务端全局设置")
    llm_polish: bool = Field(default=False, description="是否用大模型整理答案；默认只用精排与规则提取")
    web_search: bool | None = Field(default=None, description="本次是否允许联网搜索；缺省用服务端全局设置")
    reject_doc_ids: list[str] = Field(default_factory=list, description="用户否决的答案来源文档：本次回答直接排除，并累计惩罚降权")


@search_router.post("/chat")
def chat(req: ChatRequest) -> dict:
    try:
        result = answer_service.answer(
            req.question, req.history,
            fast_mode=req.fast_mode, llm_polish=req.llm_polish, web_search=req.web_search,
            rejected_doc_ids=req.reject_doc_ids,
        ).to_dict()
        # 提问频率统计（用于首页「从一个问题开始」展示最近 7 天热门问题）
        if result.get("status") != "error":
            try:
                database.record_question(req.question)
            except Exception:   # noqa: BLE001
                pass
        if req.reject_doc_ids:
            # 否决来源计入惩罚参数（持久化，后续提问持续降权）；记录失败不影响本次回答
            try:
                database.add_answer_penalties(req.reject_doc_ids, req.question)
            except Exception:   # noqa: BLE001
                pass
            extra = f"已排除你标记的 {len(req.reject_doc_ids)} 个来源并计入惩罚参数"
            result["notice"] = f"{result['notice']}；{extra}" if result.get("notice") else extra
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"服务处理异常：{type(exc).__name__}") from exc


MAX_IMAGE_BYTES = 10 * 1024 * 1024


async def _read_upload(file: UploadFile, limit: int, message: str) -> bytes:
    """只读取上限加一个字节，避免先把超大上传文件完整复制到内存。"""
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail=message)
    return content


@search_router.post("/chat/image")
async def chat_image(image: UploadFile = File(...)) -> dict:
    """上传图片 → 视觉模型读出故障现象 → 返回文字问题，前端拿去走原有问答。"""
    mime = (image.content_type or "").lower()
    if not mime.startswith("image/"):
        raise HTTPException(status_code=400, detail="请上传图片文件（png / jpg / webp 等）")
    data = await _read_upload(image, MAX_IMAGE_BYTES, "图片超过 10MB，请压缩后再上传")
    if not data:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if not settings.vision_available:
        raise HTTPException(status_code=400, detail="图片识别服务未配置，请直接用文字描述问题")
    try:
        question = await run_in_threadpool(llm.describe_image, data, mime)
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not question:
        raise HTTPException(status_code=502, detail="图片识别结果为空，请换个角度拍摄或直接文字描述")
    return {"question": question}


@search_router.get("/questions/top")
def questions_top(limit: int = 4, days: int = 7) -> dict:
    """最近 days 天提问最多的问题（首页快捷问题数据源）。"""
    return {"questions": database.top_questions(max(1, min(days, 90)), max(1, min(limit, 8)))}


@search_router.get("/health")
def health() -> dict:
    return {"status": "ok", "stats": kb.stats()}


@search_router.get("/documents")
def list_documents(category: str = "", doc_type: str = "") -> dict:
    return {
        "documents": kb.list_documents(category, doc_type),
        "categories": kb.category_counts(doc_type),
        "stats": kb.stats(),
    }


@search_router.get("/vendors")
def list_vendors() -> dict:
    """厂商清单：厂商系统类记录按厂商归类，清单在网页端维护。"""
    return {"vendors": recordmeta.list_vendors()}


@search_router.get("/categories")
def list_categories() -> dict:
    """全部可选问题类型：内置固定分类 + 后台新增的自定义类型。"""
    return {"categories": recordmeta.list_categories()}


@search_router.get("/templates")
def list_templates() -> dict:
    """各问题类型的填写模板；后台未自定义的类型返回默认模板。"""
    return {"templates": recordmeta.get_templates()}


@search_router.get("/timeline")
def timeline() -> dict:
    """按分类的问题时间线：何时遇到什么问题、何时解决了什么问题。"""
    return timeline_service.build()


@search_router.get("/documents/{doc_id}")
def get_document(doc_id: str) -> dict:
    try:
        return kb.get_document(doc_id)
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc


@search_router.get("/documents/{doc_id}/preview")
def preview_document(doc_id: str) -> dict:
    try:
        return kb.preview(doc_id)
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc


@search_router.get("/documents/{doc_id}/download")
def download_document(doc_id: str) -> FileResponse:
    """下载原始文件。查询端和管理端均提供下载；写操作仅管理端提供。"""
    try:
        path, name = kb.source_file(doc_id)
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # HTTP 头必须可按 latin-1 编码；中文原名通过 RFC 5987 的 filename* 传递。
    fallback = f"document{Path(name).suffix.lower()}"
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
    return FileResponse(str(path), headers={"Content-Disposition": disposition},
                        media_type="application/octet-stream")


# 兼容旧依赖名称；管理端写权限代表已登录，注册账号另由管理员角色控制。
require_admin = require_login


def require_super_admin(identity: dict = Depends(require_login)) -> dict:
    """仅管理员账号（role=admin）：账号管理、全库修改记录与周报生成设置。"""
    if identity.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return identity


# ---------- Admin（读写，需先登录）----------
admin_router = APIRouter(prefix="/api", tags=["admin"], dependencies=[Depends(require_admin)])


@admin_router.get("/audit-logs")
def audit_logs(limit: int = 20, page: int = 1) -> dict:
    """操作审计日志分页（时间倒序）。"""
    logs, total = audit.list_recent(limit, page)
    return {"logs": logs, "total": total, "page": page, "page_size": limit}


class MarkdownRequest(BaseModel):
    content: str = Field(default="", description="Markdown / 纯文本内容")
    doc_id: str = Field(default="", description="为空则新建，否则覆盖该文档")
    filename: str = Field(default="", description="文件名，缺省时自动生成")
    category: str = Field(default="", description="人工指定的单一问题类型（兼容）；为空时按内容自动归类")
    categories: str = Field(default="", description="多分类标签，| 或逗号分隔；非空时优先于 category")
    vendor: str = Field(default="", description="厂商（厂商系统类记录必填）")
    resolved: str = Field(default="", description="解决状态：resolved / improving / unresolved；空保留原值")
    uploaded_by_name: str = Field(default="", description="运维人用户名（默认当前登录用户名）；仅创建时写入")
    parent_id: str = Field(default="", description="追踪的父运维记录ID；仅创建时设置")


@admin_router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form("doc"),
    category: str = Form(""),
    categories: str = Form("", description="多分类标签，| 或逗号分隔；非空时优先于 category"),
    date: str = Form(""),
    vendor: str = Form(""),
    resolved: str = Form(""),
    operator_name: str = Form("", description="运维人用户名；缺省为当前登录用户名"),
    parent_id: str = Form(""),
    file_time: str = Form("", description="本地文件修改时间（YYYY-MM-DD HH:MM:SS）；作为上传时间与记录时间默认值"),
    identity: dict = Depends(require_login),
) -> dict:
    """上传文档：doc_type=doc 基础文档（默认）/ record 运维问题记录。

    category 非空时强制分类（运维问题上传时人工选择的问题类型，优先于自动判定）；
    date 为运维记录的问题发生日期（YYYY-MM-DD），入库后作为记录时间展示与时间线排序；
    未选日期时默认取 file_time 的日期部分（文件修改时间）；
    vendor 为厂商系统类记录的厂商（问题类型选「厂商系统」时必填）；
    resolved 为录入时选择的解决状态（resolved / improving / unresolved），时间线按此显示；
    operator_name 为运维人用户名，入库时与登录账号分开记录。
    """
    if doc_type not in {"doc", "record"}:
        raise HTTPException(status_code=400, detail="doc_type 只能是 doc / record")
    try:
        content = await _read_upload(file, kb.MAX_UPLOAD_BYTES, "文件超过 100MB，请拆分后再上传")
        return await run_in_threadpool(kb.upload, file.filename or "", content, doc_type=doc_type,
                                       category=category, categories=categories, date=date,
                                       vendor=vendor, resolved=resolved,
                                       uploaded_by=identity["username"],
                                       uploaded_by_name=operator_name or identity.get("display_name", ""),
                                       parent_id=parent_id, file_time=file_time)
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="关联的父记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@admin_router.post("/knowledge")
async def upload_knowledge(
    file: UploadFile = File(...),
    file_time: str = Form("", description="本地文件修改时间（YYYY-MM-DD HH:MM:SS）；作为上传时间默认值"),
    identity: dict = Depends(require_login),
) -> dict:
    """常识注入：上传 md / docx / xlsx / csv / txt，入库固定归类为「常识库」。"""
    try:
        content = await _read_upload(file, kb.MAX_UPLOAD_BYTES, "文件超过 100MB，请拆分后再上传")
        return await run_in_threadpool(kb.upload, file.filename or "", content,
                                       category=taxonomy.KNOWLEDGE_CATEGORY, uploaded_by=identity["username"],
                                       file_time=file_time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_MAX_BYTES = 10 * 1024 * 1024


@admin_router.post("/images")
async def upload_image(file: UploadFile = File(...)) -> dict:
    """编辑器插图：保存到 data/images，返回站内 URL 供 Markdown 引用。"""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="仅支持 png / jpg / gif / webp / bmp 格式图片")
    content = await _read_upload(file, IMAGE_MAX_BYTES, "图片超过 10MB 上限")
    if not content:
        raise HTTPException(status_code=400, detail="图片内容为空")
    name = f"{secrets.token_hex(10)}{ext}"
    await run_in_threadpool((settings.images_dir / name).write_bytes, content)
    return {"url": f"/images/{name}", "name": name}


@admin_router.put("/documents/{doc_id}/replace")
async def replace_document(doc_id: str, file: UploadFile = File(...)) -> dict:
    """用新文件替换已有文档，保留文档 ID 与首次上传时间。"""
    try:
        content = await _read_upload(file, kb.MAX_UPLOAD_BYTES, "文件超过 100MB，请拆分后再上传")
        return await run_in_threadpool(kb.replace_file, doc_id, file.filename or "", content)
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@admin_router.get("/documents/{doc_id}/source")
def read_source(doc_id: str) -> dict:
    try:
        return kb.read_source(doc_id)
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@admin_router.post("/documents/markdown")
def create_markdown(req: MarkdownRequest, identity: dict = Depends(require_login)) -> dict:
    try:
        return kb.save_markdown(req.content, "", req.filename, req.category, req.vendor, req.resolved,
                                categories=req.categories,
                                uploaded_by=identity["username"],
                                uploaded_by_name=req.uploaded_by_name or identity.get("display_name", ""),
                                parent_id=req.parent_id)
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="关联的父记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@admin_router.put("/documents/{doc_id}")
def update_markdown(doc_id: str, req: MarkdownRequest, identity: dict = Depends(require_login)) -> dict:
    try:
        return kb.save_markdown(req.content, doc_id, req.filename, req.category, req.vendor, req.resolved,
                                categories=req.categories,
                                uploaded_by=identity["username"], parent_id=req.parent_id,
                                edited_by=identity["username"],
                                edited_by_name=identity.get("display_name", ""))
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@admin_router.get("/documents/{doc_id}/edits")
def list_edits(doc_id: str, identity: dict = Depends(require_login)) -> dict:
    """单条记录的编辑溯源日志（新在前）。"""
    return {"edits": database.list_edit_logs(doc_id)}


@admin_router.get("/edits")
def list_all_edits(limit: int = 100, identity: dict = Depends(require_super_admin)) -> dict:
    """全库修改记录（修改记录页）：谁在何时新建 / 修改了哪条记录。"""
    return {"edits": database.list_all_edit_logs(max(1, min(limit, 300)))}


class CategoryRequest(BaseModel):
    category: str = Field(default="", description="单一分类（兼容旧客户端）")
    categories: list[str] = Field(default_factory=list, description="多分类标签；非空时优先于 category")


@admin_router.patch("/documents/{doc_id}/category")
def update_category(doc_id: str, req: CategoryRequest) -> dict:
    """人工设置分类标签（支持多选）；categories 非空时按其全量设置，否则按单值 category。"""
    try:
        tags = req.categories if req.categories else ([req.category] if req.category else [])
        return {"doc": kb.set_categories(doc_id, tags)}
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class VendorPatchRequest(BaseModel):
    vendor: str = Field(default="", description="厂商名称；空串清除厂商")


@admin_router.patch("/documents/{doc_id}/vendor")
def update_vendor(doc_id: str, req: VendorPatchRequest) -> dict:
    """人工设置 / 清除记录的厂商（厂商系统类记录按厂商归类）。"""
    try:
        return {"doc": kb.set_vendor(doc_id, req.vendor)}
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class TimeRequest(BaseModel):
    date: str = Field(default="", description="记录时间，格式 YYYY-MM-DD")


@admin_router.patch("/documents/{doc_id}/time")
def update_document_time(doc_id: str, req: TimeRequest) -> dict:
    """人工修正记录时间（年月日），用于列表显示与时间线排序。"""
    try:
        return {"doc": kb.set_document_time(doc_id, req.date)}
    except kb.DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class WebSearchRequest(BaseModel):
    enabled: bool = Field(description="true 开启联网搜索，false 关闭；立即生效")


@admin_router.get("/web-search")
def web_search_status() -> dict:
    """联网搜索开关状态：available 为配置层（有密钥），enabled 为运行时开关。"""
    return {
        "available": settings.web_search_available,
        "provider": settings.web_search_provider,
        "enabled": database.web_search_runtime_enabled(),
    }


@admin_router.post("/web-search")
def set_web_search(req: WebSearchRequest) -> dict:
    database.set_runtime_setting("web_search", "on" if req.enabled else "off")
    return {
        "available": settings.web_search_available,
        "provider": settings.web_search_provider,
        "enabled": database.web_search_runtime_enabled(),
    }


class FastModeRequest(BaseModel):
    enabled: bool = Field(description="true 快速回答（不精排、不调模型），false 高质量路径")


@admin_router.get("/fast-mode")
def fast_mode_status() -> dict:
    """快速回答开关状态：开启后问答命中知识库即回原文，单次 0.2s 量级。"""
    return {"enabled": database.fast_mode_enabled()}


@admin_router.post("/fast-mode")
def set_fast_mode(req: FastModeRequest) -> dict:
    database.set_runtime_setting("fast_mode", "on" if req.enabled else "off")
    return {"enabled": database.fast_mode_enabled()}


class VendorRequest(BaseModel):
    name: str = Field(description="厂商名称")


@admin_router.post("/vendors")
def add_vendor(req: VendorRequest) -> dict:
    """新增厂商；重名忽略。"""
    try:
        return {"vendors": recordmeta.add_vendor(req.name)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@admin_router.delete("/vendors/{name}")
def remove_vendor(name: str) -> dict:
    """删除厂商。已有文档上填的厂商值保持不变。"""
    return {"vendors": recordmeta.delete_vendor(name)}


class CategoryNameRequest(BaseModel):
    name: str = Field(description="自定义问题类型名称")


@admin_router.post("/categories")
def add_category(req: CategoryNameRequest) -> dict:
    """新增自定义问题类型；与内置类型重名时忽略。"""
    try:
        return {"categories": recordmeta.add_category(req.name)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@admin_router.delete("/categories/{name}")
def remove_category(name: str) -> dict:
    """删除自定义问题类型；已保存记录上的标签保留。"""
    try:
        return {"categories": recordmeta.delete_category(name)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class TemplateRequest(BaseModel):
    category: str = Field(description="问题类型")
    content: str = Field(default="", description="模板内容；为空表示恢复默认模板")


@admin_router.put("/templates")
def save_template(req: TemplateRequest) -> dict:
    """保存某问题类型的上传模板。"""
    try:
        return {"templates": recordmeta.save_template(req.category, req.content)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class WeeklySettingsRequest(BaseModel):
    enabled: bool = Field(description="是否启用周报定时生成")
    weekday: int = Field(description="每周几生成（1=周一 … 7=周日）")
    time: str = Field(description="生成时间点（HH:MM）")
    operators: list[str] = Field(default_factory=list, description="需要生成周报的运维人名单")


@admin_router.get("/weekly-report/settings")
def weekly_report_settings(identity: dict = Depends(require_super_admin)) -> dict:
    """周报定时生成设置 + 可勾选的运维人名单（来自历史运维记录）。"""
    return {**weekly_service.get_settings(), "owners": database.list_record_owners()}


@admin_router.put("/weekly-report/settings")
def save_weekly_settings(req: WeeklySettingsRequest, identity: dict = Depends(require_super_admin)) -> dict:
    try:
        return weekly_service.save_settings(req.enabled, req.weekday, req.time, req.operators)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@admin_router.get("/weekly-reports")
def list_weekly_reports(limit: int = 12, identity: dict = Depends(require_super_admin)) -> dict:
    """最近几周的周报（新在前），每条含运维人与本周事项列表；仅管理员可见。"""
    return {"reports": database.list_weekly_reports(max(1, min(limit, 52)))}


@admin_router.post("/weekly-reports/generate")
def generate_weekly_reports(identity: dict = Depends(require_super_admin)) -> dict:
    """按当前名单立即生成本周周报（重复生成会覆盖本周结果）。"""
    return {"reports": weekly_service.generate_reports()}


@admin_router.delete("/documents/{doc_id}")
def delete_document(doc_id: str) -> dict:
    try:
        if not kb.delete(doc_id):
            raise HTTPException(status_code=404, detail="文档不存在")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": doc_id, "stats": kb.stats()}


@admin_router.post("/documents/rebuild")
def rebuild_index() -> dict:
    return kb.rebuild()


class EventStatusRequest(BaseModel):
    chunk_id: str = Field(description="事件对应的时间线分块 ID")
    doc_id: str = Field(default="", description="所属文档 ID，便于文档删除时清理标记")
    status: str = Field(default="", description="resolved / unresolved / unknown；空串清除标记，恢复按内容判定")


@admin_router.patch("/timeline/event-status")
def update_event_status(req: EventStatusRequest) -> dict:
    """人工标记时间线事件的完成情况，覆盖语义判定。"""
    status = req.status.strip()
    if status and status not in database.EVENT_STATUSES:
        raise HTTPException(status_code=400, detail=f"未知状态：{status}")
    database.set_event_status(req.chunk_id, req.doc_id, status)
    return {"chunk_id": req.chunk_id, "status": status}

