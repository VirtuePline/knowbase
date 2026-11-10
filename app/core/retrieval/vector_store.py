"""MongoDB 向量持久化 + FAISS 内存检索。

向量仍以 double 数组存在 MongoDB chunk_vectors 集合（_id = chunk_id），随文档增删
同步维护，content_hash 标识嵌入输入以检测原地编辑。检索用 FAISS IndexFlatIP：
向量与查询都做 L2 归一化后取内积，即余弦相似度；首次检索把该模型的向量全量载入
内存并构建索引，后续查询毫秒级完成，不再每次从 MongoDB 拉全量向量算 numpy 点积。
upsert / prune / 切换集合都会使内存索引失效（版本号递增），下一次检索自动重建。
faiss-cpu 未安装或索引构建失败时自动回退 numpy 余弦实现，检索不中断。
"""
from __future__ import annotations

import threading

import numpy as np
from pymongo import ASCENDING, ReplaceOne

from app.services import mongo

try:
    import faiss
except ImportError:   # faiss-cpu 未安装：回退 numpy 全量余弦
    faiss = None

_COLLECTION = "chunk_vectors"


class VectorStore:
    def __init__(self, mongo_uri: str = "", mongo_db: str = "",
                 collection: str = _COLLECTION) -> None:
        # 构造单例时不访问数据库；首次真正读写向量时才连接并确保索引。
        self._mongo_uri = mongo_uri
        self._mongo_db = mongo_db
        self._collection_name = collection
        self._collection = None
        self._collection_key: tuple[int, str] | None = None
        self._collection_lock = threading.Lock()
        # FAISS 内存索引缓存：(model, 版本号) 命中即直接查询，不再读库
        self._faiss_lock = threading.Lock()
        self._version = 0
        self._faiss_index = None          # faiss.IndexFlatIP
        self._faiss_ids: list[str] = []
        self._faiss_key: tuple[str, int] | None = None

    @property
    def _col(self):
        # 缺省配置在访问时解析；测试切换临时库时也不能继续使用原库的集合。
        database = mongo.db(self._mongo_uri, self._mongo_db)
        key = (id(database.client), database.name)
        with self._collection_lock:
            if self._collection_key != key:
                collection = database[self._collection_name]
                collection.create_index([("model", ASCENDING)])
                self._collection = collection
                self._collection_key = key
                self._version += 1        # 集合变了，内存索引作废
            return self._collection

    def existing_ids(self, model: str) -> set[str]:
        return set(self._col.distinct("_id", {"model": model}))

    def existing_hashes(self, model: str) -> dict[str, str]:
        """缺少哈希的旧记录返回空串，下一次同步会自动重新嵌入。"""
        return {row["_id"]: row.get("content_hash", "")
                for row in self._col.find({"model": model}, {"content_hash": 1})}

    def upsert(self, entries: list[tuple[str, str, list[float]]], model: str,
               content_hashes: dict[str, str] | None = None) -> None:
        """写入 / 覆盖分块向量；更换模型时同 chunk_id 的记录一并替换。"""
        if not entries:
            return
        self._col.bulk_write([
            ReplaceOne(
                {"_id": cid},
                {"_id": cid, "doc_id": did, "model": model,
                 "vector": [float(x) for x in vec],
                 "content_hash": (content_hashes or {}).get(cid, "")},
                upsert=True,
            )
            for cid, did, vec in entries
        ])
        self._version += 1

    def prune(self, valid_ids: set[str]) -> None:
        """删除已不在索引里的分块向量。"""
        stale = [cid for cid in self._col.distinct("_id") if cid not in valid_ids]
        if stale:
            self._col.delete_many({"_id": {"$in": stale}})
            self._version += 1

    @staticmethod
    def backend() -> str:
        return "faiss" if faiss is not None else "numpy"

    def search(self, query: list[float], top_k: int, model: str) -> list[tuple[str, float]]:
        """余弦相似度检索，返回 [(chunk_id, 相似度)]，按相似度降序。"""
        key = (model, self._version)
        if faiss is not None and self._faiss_key == key and self._faiss_index is not None:
            return self._query_faiss(query, top_k)
        with self._faiss_lock:
            if self._faiss_key == key and self._faiss_index is not None:
                return self._query_faiss(query, top_k)   # 并发下已被其他线程重建
            rows = list(self._col.find({"model": model}, {"vector": 1}))
            if not rows:
                return []
            ids = [r["_id"] for r in rows]
            matrix = np.array([r["vector"] for r in rows], dtype=np.float32)
            if matrix.ndim != 2 or matrix.shape[0] == 0:
                return []   # 向量维度不齐（异常数据）：宁可不返回也不让检索中断
            if faiss is not None and matrix.shape[1] == len(np.asarray(query)):
                try:
                    index = faiss.IndexFlatIP(matrix.shape[1])
                    vectors = np.ascontiguousarray(matrix, dtype=np.float32)
                    faiss.normalize_L2(vectors)   # 归一化后内积 = 余弦相似度
                    index.add(vectors)
                    self._faiss_index = index
                    self._faiss_ids = ids
                    self._faiss_key = key
                    return self._query_faiss(query, top_k)
                except Exception:   # FAISS 构建失败：本次回退 numpy，不缓存坏索引
                    self._faiss_index = None
                    self._faiss_key = None
        return self._search_numpy(matrix, ids, np.asarray(query, dtype=np.float32), top_k)

    def _query_faiss(self, query: list[float], top_k: int) -> list[tuple[str, float]]:
        q = np.ascontiguousarray(np.asarray(query, dtype=np.float32).reshape(1, -1))
        faiss.normalize_L2(q)
        k = min(top_k, self._faiss_index.ntotal)
        if k <= 0:
            return []
        sims, idx = self._faiss_index.search(q, k)
        ids = self._faiss_ids
        return [(ids[i], float(s)) for s, i in zip(sims[0], idx[0]) if 0 <= i < len(ids)]

    @staticmethod
    def _search_numpy(matrix: np.ndarray, ids: list[str],
                      q: np.ndarray, top_k: int) -> list[tuple[str, float]]:
        norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(q) or 1.0)
        norms[norms == 0] = 1.0
        sims = (matrix @ q) / norms
        order = np.argsort(-sims)[:top_k]
        return [(ids[i], float(sims[i])) for i in order]

    def count(self, model: str = "") -> int:
        return self._col.count_documents({"model": model} if model else {})
