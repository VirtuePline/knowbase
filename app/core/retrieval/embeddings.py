"""文本向量嵌入：默认本地 fastembed 模型，也可切换 OpenAI 兼容 /embeddings 服务。

local：BAAI/bge-small-zh-v1.5（512 维，ONNX 本地推理，首次使用自动下载模型，之后离线可用）。
api：任何 OpenAI 兼容的 embeddings 接口（DeepSeek 官方没有该接口，需换其他服务）。

两种方式的失败都抛 EmbeddingError，由检索层降级为纯 BM25，不影响问答。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from app.config import settings
from app.core import network_policy


class EmbeddingError(Exception):
    pass


class LocalEmbedder:
    """fastembed 本地模型；模型懒加载，首次调用时加载（必要时自动下载）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from fastembed import TextEmbedding
                        # 缓存固定到项目内 data/models/fastembed：预下载后本机与
                        # Docker 镜像共用同一离线模型，运行时不再联网下载
                        cache_dir = str(settings.base_dir / "data" / "models" / "fastembed")
                        self._model = TextEmbedding(settings.embedding_model, cache_dir=cache_dir,
                                                    local_files_only=not network_policy.external_apis_enabled())
                    except Exception as exc:   # 模型下载失败、依赖缺失等
                        raise EmbeddingError(f"本地向量模型加载失败：{type(exc).__name__}: {exc}") from exc
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return [v.tolist() for v in self._load().embed(texts)]
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"向量化失败：{type(exc).__name__}: {exc}") from exc

    def query(self, text: str) -> list[float]:
        # bge 系列检索查询需带指令前缀，fastembed 的 query_embed 已内置
        try:
            return next(iter(self._load().query_embed(text))).tolist()
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"查询向量化失败：{type(exc).__name__}: {exc}") from exc


class ApiEmbedder:
    """OpenAI 兼容 embeddings 接口。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not network_policy.external_apis_enabled():
            raise EmbeddingError("外部接口已关闭，API 向量服务已停用")
        if not settings.embedding_api_key:
            raise EmbeddingError("未配置 embedding.api_key")
        url = f"{settings.embedding_base_url}/embeddings"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.embedding_api_key}",
        }
        payload = {"model": settings.embedding_model, "input": texts}
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            if not network_policy.external_apis_enabled():
                raise EmbeddingError("外部接口已关闭，API 向量服务已停用")
            with urllib.request.urlopen(request, timeout=settings.embedding_timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            rows = sorted(data["data"], key=lambda item: item["index"])
            return [row["embedding"] for row in rows]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError(f"嵌入接口调用失败：{type(exc).__name__}: {exc}") from exc

    def query(self, text: str) -> list[float]:
        return self.embed([text])[0]


_embedder = None
_embedder_lock = threading.Lock()


def get_embedder():
    """按配置返回嵌入器单例；embedding.enabled = false 时返回 None。"""
    global _embedder
    if not settings.embedding_enabled or (settings.embedding_provider != "local"
                                          and not network_policy.external_apis_enabled()):
        return None
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                _embedder = LocalEmbedder() if settings.embedding_provider == "local" else ApiEmbedder()
    return _embedder
