"""MongoDB 连接管理（进程内按连接串复用连接池）。

所有模块经此访问数据库，settings 的引用集中在这里——测试把 app.* 模块的
settings 换成临时配置后，库名 / 连接地址随之切换，无需改其他模块。
"""
from __future__ import annotations

from pymongo import MongoClient
from pymongo.errors import PyMongoError  # noqa: F401  (调用方可从此捕获)

from app.config import settings

_clients: dict[str, MongoClient] = {}


def client(uri: str = "") -> MongoClient:
    """按连接串复用 MongoClient；缺省取配置 database.uri。"""
    target = uri or settings.mongo_uri
    if target not in _clients:
        _clients[target] = MongoClient(target, serverSelectionTimeoutMS=5000)
    return _clients[target]


def db(uri: str = "", name: str = ""):
    """取数据库对象；缺省取配置 database.name（knowbase）。"""
    return client(uri)[name or settings.mongo_db]


def ping() -> None:
    """连通性自检：服务启动 / 建库时调用，连不上立即报错。"""
    client().admin.command("ping")
