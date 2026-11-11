"""全局配置。读取项目根目录的 config.yaml，并用 config.local.yaml 覆盖私有项。

config.local.yaml 用于本机密钥等不应提交的内容；两层配置按字典深度合并，
未写出的项沿用 config.yaml 的值。修改后需重启服务生效。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.yaml"
LOCAL_CONFIG_FILE = BASE_DIR / "config.local.yaml"


class ConfigError(Exception):
    """配置文件缺失或格式错误。启动阶段直接报出，避免带着错误配置运行。"""


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} 不是合法的 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} 顶层必须是键值映射")
    return data


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _section(data: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    """取出一个配置段，并拒绝拼错的键——静默忽略会让现场以为改了参数。"""
    value = data.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"config.yaml 中 {name} 必须是键值映射")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"config.yaml 中 {name} 存在未知配置项：{', '.join(sorted(unknown))}")
    return value


def _int(section: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(section.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {key} 必须是整数，当前为 {section.get(key)!r}") from exc


def _float(section: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(section.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {key} 必须是数字，当前为 {section.get(key)!r}") from exc


def _bool(section: dict[str, Any], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    raise ConfigError(f"配置项 {key} 必须是 true / false，当前为 {value!r}")


def _str(section: dict[str, Any], key: str, default: str) -> str:
    value = section.get(key, default)
    if value is None:
        return default
    return str(value).strip()


@dataclass(frozen=True)
class Settings:
    base_dir: Path
    raw_dir: Path
    index_dir: Path
    chunks_dir: Path
    index_file: Path
    web_dir: Path
    images_dir: Path

    chunk_size: int
    chunk_overlap: int
    excel_rows_per_chunk: int
    top_k: int
    min_coverage: float
    semantic_threshold: float
    semantic_floor: float
    max_excerpt_chars: int
    max_context_chars: int

    embedding_enabled: bool
    embedding_provider: str
    embedding_model: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_timeout: int
    mongo_uri: str
    mongo_db: str

    rerank_enabled: bool
    rerank_threshold: float
    rerank_candidates: int

    analysis_use_llm: bool
    analysis_max_section_chars: int
    analysis_batch_chars: int

    llm_model: str
    llm_base_url: str
    llm_max_tokens: int
    llm_timeout: int
    llm_api_key: str

    vision_model: str
    vision_base_url: str
    vision_api_key: str

    host: str
    port: int
    admin_host: str
    admin_port: int
    admin_password: str

    auth_enabled: bool
    auth_admin_user: str
    auth_admin_password: str

    web_search_enabled: bool = True
    web_search_provider: str = "deepseek"   # "deepseek" 用 DeepSeek Search API | "tavily" 用 Tavily
    web_search_api_key: str = ""
    web_search_base_url: str = "https://api.deepseek.com"
    web_search_timeout: int = 15
    web_search_max_results: int = 5

    @property
    def llm_available(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def vision_available(self) -> bool:
        return bool(self.vision_api_key)

    @property
    def web_search_available(self) -> bool:
        return self.web_search_enabled and bool(self.web_search_api_key)

    @property
    def supported_exts(self) -> set[str]:
        return {".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".txt", ".md"}


def load_settings(config_file: Path = CONFIG_FILE, local_file: Path = LOCAL_CONFIG_FILE) -> Settings:
    if not config_file.is_file():
        raise ConfigError(f"缺少配置文件：{config_file}（可从仓库中恢复默认值）")

    data = _read_yaml(config_file)
    if local_file.is_file():
        data = _merge(data, _read_yaml(local_file))

    llm = _section(data, "llm", {"model", "base_url", "api_key", "max_tokens", "timeout"})
    retrieval = _section(data, "retrieval", {"top_k", "min_coverage", "semantic_threshold", "semantic_floor", "max_context_chars", "max_excerpt_chars"})
    chunking = _section(data, "chunking", {"chunk_size", "chunk_overlap", "excel_rows_per_chunk"})
    analysis = _section(data, "analysis", {"use_llm", "max_section_chars", "batch_chars"})
    server = _section(data, "server", {"host", "port", "admin_host", "admin_port", "admin_password"})
    embedding = _section(data, "embedding", {"enabled", "provider", "model", "base_url", "api_key", "timeout"})
    rerank = _section(data, "rerank", {"enabled", "threshold", "candidates"})
    dbcfg = _section(data, "database", {"uri", "name"})
    auth = _section(data, "auth", {"enabled", "admin_user", "admin_password"})
    vision = _section(data, "vision", {"model", "base_url", "api_key"})
    web_search = _section(data, "web_search", {"enabled", "provider", "api_key", "base_url", "timeout", "max_results"})

    web_search_provider = _str(web_search, "provider", "deepseek").lower()
    if web_search_provider not in {"deepseek", "tavily"}:
        raise ConfigError(f"web_search.provider 只能是 deepseek / tavily，当前为 {web_search_provider}")
    web_search_timeout = _int(web_search, "timeout", 15)
    if not 1 <= web_search_timeout <= 120:
        raise ConfigError("web_search.timeout 必须在 1~120 秒之间")
    web_search_max_results = _int(web_search, "max_results", 5)
    if not 1 <= web_search_max_results <= 20:
        raise ConfigError("web_search.max_results 必须在 1~20 之间")
    default_search_url = ("https://api.deepseek.com" if web_search_provider == "deepseek"
                          else "https://api.tavily.com")
    web_search_base_url = _str(web_search, "base_url", default_search_url).rstrip("/")
    try:
        parsed_search_url = urlsplit(web_search_base_url)
        valid_search_url = (
            parsed_search_url.scheme in {"http", "https"}
            and bool(parsed_search_url.hostname)
            and parsed_search_url.username is None and parsed_search_url.password is None
            and not parsed_search_url.query and not parsed_search_url.fragment
            and not any(c.isspace() for c in web_search_base_url)
        )
        parsed_search_url.port  # 校验非法端口；错误信息不能回显可能含密钥的 URL。
    except ValueError:
        valid_search_url = False
    if not valid_search_url:
        raise ConfigError("web_search.base_url 必须是不含账号、查询参数和片段的 HTTP(S) 地址")

    min_coverage = _float(retrieval, "min_coverage", 0.32)
    if not 0 <= min_coverage <= 1:
        raise ConfigError(f"retrieval.min_coverage 必须在 0~1 之间，当前为 {min_coverage}")

    semantic_threshold = _float(retrieval, "semantic_threshold", 0.6)
    if not 0 <= semantic_threshold <= 1:
        raise ConfigError(f"retrieval.semantic_threshold 必须在 0~1 之间，当前为 {semantic_threshold}")

    semantic_floor = _float(retrieval, "semantic_floor", 0.45)
    if not 0 <= semantic_floor <= semantic_threshold:
        raise ConfigError(f"retrieval.semantic_floor 必须在 0~semantic_threshold({semantic_threshold}) 之间，当前为 {semantic_floor}")

    embedding_provider = _str(embedding, "provider", "local").lower()
    if embedding_provider not in {"local", "api"}:
        raise ConfigError(f"embedding.provider 只能是 local / api，当前为 {embedding_provider}")

    chunk_size = _int(chunking, "chunk_size", 600)
    chunk_overlap = _int(chunking, "chunk_overlap", 100)
    if chunk_size <= 0:
        raise ConfigError(f"chunking.chunk_size 必须大于 0，当前为 {chunk_size}")
    if not 0 <= chunk_overlap < chunk_size:
        raise ConfigError(f"chunking.chunk_overlap 必须大于等于 0 且小于 chunk_size，当前为 {chunk_overlap}")

    max_section_chars = _int(analysis, "max_section_chars", 3000)
    if max_section_chars < 200:
        raise ConfigError(f"analysis.max_section_chars 不能小于 200，当前为 {max_section_chars}")

    port = _int(server, "port", 8000)
    admin_port = _int(server, "admin_port", 8001)
    if port == admin_port:
        raise ConfigError(f"server.port 与 server.admin_port 不能相同（当前均为 {port}），查询端与管理端必须分开")

    # 环境变量优先于配置文件：容器内监听 0.0.0.0 才能映射端口，
    # 而同一份 config.yaml 在宿主机保持 127.0.0.1 仅本机可用
    host = os.environ.get("KB_HOST", "").strip() or _str(server, "host", "127.0.0.1")
    admin_host = os.environ.get("KB_ADMIN_HOST", "").strip() or _str(server, "admin_host", "127.0.0.1")

    raw_dir = BASE_DIR / "data" / "raw"
    index_dir = BASE_DIR / "data" / "index"
    images_dir = BASE_DIR / "data" / "images"
    for d in (raw_dir, index_dir, index_dir / "chunks", images_dir):
        d.mkdir(parents=True, exist_ok=True)

    return Settings(
        base_dir=BASE_DIR,
        raw_dir=raw_dir,
        index_dir=index_dir,
        chunks_dir=index_dir / "chunks",
        index_file=index_dir / "bm25.json",
        web_dir=BASE_DIR / "web",
        images_dir=images_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        excel_rows_per_chunk=_int(chunking, "excel_rows_per_chunk", 12),
        top_k=_int(retrieval, "top_k", 6),
        min_coverage=min_coverage,
        semantic_threshold=semantic_threshold,
        semantic_floor=semantic_floor,
        embedding_enabled=_bool(embedding, "enabled", True),
        embedding_provider=embedding_provider,
        embedding_model=_str(embedding, "model", "BAAI/bge-small-zh-v1.5"),
        embedding_base_url=(_str(embedding, "base_url", "") or _str(llm, "base_url", "https://api.deepseek.com/v1")).rstrip("/"),
        embedding_api_key=_str(embedding, "api_key", "") or _str(llm, "api_key", ""),
        embedding_timeout=_int(embedding, "timeout", 60),
        # 环境变量优先：容器内通过 KB_MONGO_URI 指向宿主机 MongoDB
        mongo_uri=os.environ.get("KB_MONGO_URI", "").strip() or _str(dbcfg, "uri", "mongodb://127.0.0.1:27017"),
        mongo_db=_str(dbcfg, "name", "knowbase"),
        # enabled 保留旧配置兼容；管理端始终要求登录，不再由此字段绕过认证。
        auth_enabled=_bool(auth, "enabled", True),
        auth_admin_user=_str(auth, "admin_user", "admin"),
        # auth.admin_password 缺省回退 server.admin_password（兼容旧配置）
        auth_admin_password=_str(auth, "admin_password", "") or _str(server, "admin_password", ""),
        rerank_enabled=_bool(rerank, "enabled", True),
        rerank_threshold=_float(rerank, "threshold", 0.5),
        rerank_candidates=_int(rerank, "candidates", 10),
        max_excerpt_chars=_int(retrieval, "max_excerpt_chars", 900),
        max_context_chars=_int(retrieval, "max_context_chars", 14000),
        analysis_use_llm=_bool(analysis, "use_llm", True),
        analysis_max_section_chars=max_section_chars,
        analysis_batch_chars=_int(analysis, "batch_chars", 12000),
        llm_model=_str(llm, "model", "deepseek-flash"),
        llm_base_url=_str(llm, "base_url", "https://api.deepseek.com/v1").rstrip("/"),
        llm_max_tokens=_int(llm, "max_tokens", 2000),
        llm_timeout=_int(llm, "timeout", 60),
        llm_api_key=_str(llm, "api_key", ""),
        vision_model=_str(vision, "model", "qwen-vl-plus"),
        vision_base_url=_str(vision, "base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/"),
        vision_api_key=_str(vision, "api_key", ""),
        host=host,
        port=port,
        admin_host=admin_host,
        admin_port=admin_port,
        admin_password=_str(server, "admin_password", ""),
        web_search_enabled=_bool(web_search, "enabled", True),
        web_search_provider=web_search_provider,
        web_search_api_key=(
            os.environ.get("KB_WEB_SEARCH_API_KEY", "").strip()
            or _str(web_search, "api_key", "")
            # DeepSeek 搜索与对话共用同一个 API Key：未单独配置时复用 llm.api_key
            or (_str(llm, "api_key", "") if web_search_provider == "deepseek" else "")
        ),
        web_search_base_url=web_search_base_url,
        web_search_timeout=web_search_timeout,
        web_search_max_results=web_search_max_results,
    )


settings = load_settings()
