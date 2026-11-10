"""本地重排序（rerank）：bge-reranker-base 的 int8 ONNX 量化模型。

对「问题 + 候选分块」逐对打相关性分（Cross-Encoder），比单向量的余弦相似度更准：
向量检索负责召回前 N 个候选，rerank 负责把它们按与问题的真实相关度排序。

模型文件在 data/models/bge-reranker-base/（onnx/model_quantized.onnx + tokenizer.json），
推理用已安装的 onnxruntime + tokenizers，全程本地离线。加载或推理失败抛 RerankError，
由检索层降级为 RRF 排序，不影响问答。
"""
from __future__ import annotations

import threading

import numpy as np

from app.config import settings

# 输入文本对的最大长度（bge-reranker-base 支持 512 token）
_MAX_LEN = 512
# 单批打分数量，避免内存峰值
_BATCH = 8


class RerankError(Exception):
    pass


class Reranker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session = None
        self._tokenizer = None

    def _load(self):
        if self._session is None:
            with self._lock:
                if self._session is None:
                    try:
                        import onnxruntime as ort
                        from tokenizers import Tokenizer
                        model_dir = settings.base_dir / "data" / "models" / "bge-reranker-base"
                        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
                        self._tokenizer.enable_truncation(max_length=_MAX_LEN)
                        self._tokenizer.enable_padding()   # 批内对齐到最长句，pad 用模型自带定义
                        self._session = ort.InferenceSession(
                            str(model_dir / "onnx" / "model_quantized.onnx"),
                            providers=["CPUExecutionProvider"],
                        )
                    except Exception as exc:
                        raise RerankError(f"rerank 模型加载失败：{type(exc).__name__}: {exc}") from exc
        return self._session

    def score(self, query: str, texts: list[str]) -> list[float]:
        """返回每个候选与问题的相关性概率（0~1，sigmoid 归一化）。

        按文本长度排序后再分批：批内 padding 对齐到最长句，长短混批会让短文本也按
        最长句计算，实测 6 条候选（含 1 条台账长文）耗时从 6.2s 降到 1.5s 左右。
        """
        if not texts:
            return []
        session = self._load()
        input_names = {i.name for i in session.get_inputs()}
        scores = [0.0] * len(texts)
        # 短文本优先：先算一批短句，长文单独成批，避免相互拖慢
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        try:
            for start in range(0, len(order), _BATCH):
                picks = order[start:start + _BATCH]
                encoded = self._tokenizer.encode_batch(
                    [(query, texts[i]) for i in picks])
                feeds = {
                    "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
                }
                if "token_type_ids" in input_names:
                    feeds["token_type_ids"] = np.array([e.type_ids for e in encoded], dtype=np.int64)
                logits = session.run(None, feeds)[0].reshape(-1)
                probs = (1.0 / (1.0 + np.exp(-logits))).tolist()
                for i, prob in zip(picks, probs):
                    scores[i] = float(prob)
        except RerankError:
            raise
        except Exception as exc:
            raise RerankError(f"rerank 推理失败：{type(exc).__name__}: {exc}") from exc
        return scores


_reranker = None
_reranker_lock = threading.Lock()


def get_reranker():
    """rerank 单例；rerank.enabled = false 时返回 None。"""
    global _reranker
    if not settings.rerank_enabled:
        return None
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                _reranker = Reranker()
    return _reranker
