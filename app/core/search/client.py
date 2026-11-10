"""联网搜索客户端（DeepSeek Search / Tavily），只发送脱敏后的问题并读取搜索摘要，不抓取网页。

DeepSeek Search：POST {base_url}/v1/tools/search，Bearer 鉴权，响应 results[].summary 为长摘要。
Tavily：POST {base_url}/search，响应 results[].content 为摘要。
禁用、无密钥或空问题时不请求网络；请求和协议错误使用不含敏感内容的异常。
"""
from __future__ import annotations

import ipaddress
import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import settings

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_SECRET_FIELD = re.compile(
    r"(?:password|passwd|pwd|token|api[_-]?key|secret|authorization|密码|口令|密钥|令牌)"
    r"\s*[:=：]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,，;；]+)", re.I,
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_API_TOKEN = re.compile(r"\b(?:sk-|tvly-)[A-Za-z0-9_-]{8,}\b", re.I)
_MAC = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])", re.I)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?::\d{1,5})?(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:]*(?:%[\w.-]+)?(?:/\d{1,3})?(?![0-9a-f:])", re.I)


@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    content: str
    score: float = 0.0


class WebSearchError(Exception):
    """网络搜索不可用；消息不包含查询、密钥或服务端原始响应。"""


def sanitize_query(question: str) -> str:
    """删除常见凭据字段、私有 IP 与 MAC，保留故障现象和公开错误码。"""
    if not isinstance(question, str):
        return ""
    text = _BEARER.sub(" ", question)
    text = _SECRET_FIELD.sub(" ", text)
    text = _API_TOKEN.sub(" ", text)
    text = _MAC.sub(" ", text)

    def redact_address(match: re.Match) -> str:
        value = match.group()
        address = value.split("/")[0]
        if "." in address:
            address = address.split(":")[0]
        try:
            return value if ipaddress.ip_address(address).is_global else " "
        except ValueError:
            return value

    text = _IPV4.sub(redact_address, text)
    text = _IPV6.sub(redact_address, text)
    return " ".join(text.split()).strip(" ,，;；")[:1000]


def _valid_result_url(value: object) -> bool:
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def _request_spec(query: str) -> tuple[str, dict]:
    """按提供方给出 (请求地址, 请求体)。两家都只接收问题本身，不传历史或文档原文。"""
    base = settings.web_search_base_url.rstrip("/")
    limit = settings.web_search_max_results
    if settings.web_search_provider == "deepseek":
        # need_content=true 才返回网页正文；summary（约 600~800 字）已足够给模型阅读
        return f"{base}/v1/tools/search", {"query": query, "count": limit, "need_content": True}
    return f"{base}/search", {
        "query": query,
        "search_depth": "basic",
        "max_results": limit,
        "include_answer": False,
        "include_raw_content": False,
    }


def _result_text(row: dict) -> str:
    """取结果摘要：DeepSeek 用 summary（长摘要），缺少时退回 content / snippet；Tavily 用 content。"""
    for field in ("summary", "content", "snippet"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _result_score(row: dict) -> float:
    """DeepSeek 用 rerank_score（0~3），Tavily 用 score（0~1）。"""
    for field in ("rerank_score", "score"):
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        return float(value)
    return 0.0


def search(question: str) -> list[WebResult]:
    """搜索公开参考；不接收历史或知识库原文，不调用服务端自动回答。"""
    if not settings.web_search_available:
        return []
    query = sanitize_query(question)
    if not query:
        return []
    url, payload = _request_spec(query)
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.web_search_api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=settings.web_search_timeout) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise WebSearchError("联网搜索响应超出大小限制")
        data = json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise WebSearchError(f"联网搜索服务返回 HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise WebSearchError("联网搜索连接失败或超时") from None
    except (ValueError, TypeError):
        raise WebSearchError("联网搜索响应格式无效") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise WebSearchError("联网搜索响应结构无效")

    results: list[WebResult] = []
    seen: set[str] = set()
    for row in data["results"]:
        if not isinstance(row, dict):
            continue
        row_url, title = row.get("url"), row.get("title")
        content = _result_text(row)
        if (not _valid_result_url(row_url) or row_url in seen
                or not content or not isinstance(title, str)):
            continue
        results.append(WebResult(title=title.strip()[:300] or row_url, url=row_url,
                                 content=content[:3000], score=_result_score(row)))
        seen.add(row_url)
        if len(results) >= settings.web_search_max_results:
            break
    return results
