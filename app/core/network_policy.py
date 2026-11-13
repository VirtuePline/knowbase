"""全局出站开关。每次调用读取持久化设置，数据库不可读时拒绝外部请求。"""
from __future__ import annotations


def external_apis_enabled() -> bool:
    # 延迟导入以避免配置、入库与数据库服务之间的初始化循环。
    from app.services import database
    try:
        return database.external_apis_enabled() is True
    except Exception:
        return False
