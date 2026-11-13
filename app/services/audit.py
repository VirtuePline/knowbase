"""操作审计日志：管理端每次写操作记录 IP、操作时间、动作与账号，存 MongoDB audit_logs。

由管理端 HTTP 中间件调用：方法非 GET 的 /api 请求即视为一次操作；
动作名称按「方法 + 路径」映射（路径参数归一化），从 JSON 请求体提取关键摘要。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from app.services import auth, database, mongo

# (方法, 路径模板, 动作名)。{xxx} 匹配任意非 / 片段。
_ACTION_RULES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/api/admin/login", "登录管理端"),
    ("POST", "/api/admin/register", "注册账号"),
    ("POST", "/api/admin/change-password", "修改本人密码"),
    ("POST", "/api/admin/logout", "退出管理端"),
    ("POST", "/api/departments", "创建部门"),
    ("PUT", "/api/departments/{id}", "重命名部门"),
    ("DELETE", "/api/departments/{id}", "删除部门"),
    ("PATCH", "/api/admin/users/{id}/departments", "调整部门角色"),
    ("PUT", "/api/weekly-report/my-settings", "保存本人周报设置"),
    ("PUT", "/api/weekly-report/settings", "保存统一周报设置"),
    ("POST", "/api/weekly-reports/generate", "生成工作周报"),
    ("POST", "/api/external-apis", "调整外部接口开关"),
    ("POST", "/api/documents", "上传文档"),
    ("PUT", "/api/documents/{id}/replace", "替换文档"),
    ("DELETE", "/api/documents/{id}", "删除文档"),
    ("PATCH", "/api/documents/{id}/category", "修正文档分类"),
    ("PATCH", "/api/documents/{id}/fields", "保存文档字段"),
    ("POST", "/api/documents/markdown", "保存工作记录"),
    ("PUT", "/api/documents/{id}", "编辑工作记录"),
    ("POST", "/api/documents/rebuild", "重建索引"),
    ("POST", "/api/images", "上传图片"),
    ("POST", "/api/fields", "新建字段"),
    ("PUT", "/api/fields/{key}", "修改字段"),
    ("DELETE", "/api/fields/{key}", "删除字段"),
    ("POST", "/api/events/status", "标记时间线事件"),
    ("PUT", "/api/templates/{name}", "保存模板"),
    ("POST", "/api/vendors", "添加厂商"),
    ("DELETE", "/api/vendors/{name}", "删除厂商"),
)

_REGEX_CACHE: dict[str, re.Pattern] = {}

# 从 JSON 请求体提取摘要的字段（按优先级）
_DETAIL_KEYS = ("name", "username", "vendor", "category", "chunk_id", "key", "label", "doc_id", "target_department_id")


def action_name(method: str, path: str) -> str:
    """方法 + 路径 → 可读动作名；未登记的接口原样返回「METHOD path」。"""
    for m, tpl, name in _ACTION_RULES:
        if m != method:
            continue
        regex = _REGEX_CACHE.get(tpl)
        if regex is None:
            regex = re.compile("^" + re.sub(r"\{[^}/]+\}", r"[^/]+", tpl) + "$")
            _REGEX_CACHE[tpl] = regex
        if regex.match(path):
            return name
    return f"{method} {path}"


def detail_from_body(body: bytes, content_type: str) -> str:
    """JSON 请求体提取关键摘要；文件上传等非 JSON 请求返回空。"""
    if "application/json" not in content_type or not body:
        return ""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in _DETAIL_KEYS:
        value = data.get(key)
        if value:
            return f"{key}={value}"
    content = data.get("content")
    if content:
        return f"content={str(content)[:80]}"
    return ""


def auth_detail_from_body(body: bytes, content_type: str) -> str:
    """账号接口仅记录标识字段，不回退到内容、旧密码或新密码。"""
    if "application/json" not in content_type or not body:
        return ""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return " · ".join(f"{key}={data[key][:64]}" for key in ("account", "username")
                      if isinstance(data.get(key), str) and data[key])


def client_ip(request) -> str:
    """客户端 IP：反代场景优先取 X-Forwarded-For 的第一段。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def record(ip: str, method: str, path: str, status: int,
           detail: str = "", user: str = "") -> None:
    """写入一条审计日志；日志失败不影响业务请求。"""
    try:
        mongo.db().audit_logs.insert_one({
            "time": database.now_iso(),
            "ip": ip,
            "user": user or "-",
            "method": method,
            "path": path,
            "action": action_name(method, path),
            "status": int(status),
            "detail": str(detail)[:200],
        })
    except Exception:   # noqa: BLE001 审计失败不阻塞业务
        pass


def list_recent(limit: int = 20, page: int = 1) -> tuple[list[dict], int]:
    """操作日志分页（_id 单调递增，倒序即最新在前）。返回 (当前页记录, 总条数)。"""
    coll = mongo.db().audit_logs
    total = coll.count_documents({})
    page = max(1, int(page))
    size = max(1, min(int(limit), 100))
    rows = coll.find({}, {"_id": 0}).sort("_id", -1).skip((page - 1) * size).limit(size)
    return list(rows), total


def session_user(token: str | None) -> str:
    """会话 token 对应的账号（免登录模式返回空）。"""
    return auth.session_user(token)
