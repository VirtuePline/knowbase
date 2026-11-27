"""保存生产查询模板及每次执行的输入和状态。SQL 展示文本不参与执行。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.core.production import store
from app.core.production.intent import ProductionQuery
from app.services import database, mongo


class RouteUnavailable(Exception):
    """路由或执行历史暂不可用。"""


class RouteDisabled(Exception):
    """管理员已停用该生产查询模板。"""


@dataclass(frozen=True)
class PreparedRoute:
    id: str
    plan: ProductionQuery


def prepare_route(plan: ProductionQuery, question: str, settings: Settings) -> PreparedRoute:
    """相同指标、分组与筛选字段共用模板；取出模板后重新绑定本次输入。"""
    try:
        dialect = make_url(settings.production_sql_url).get_backend_name()
    except (SQLAlchemyError, ValueError):
        raise store.ProductionStoreError("生产报表的数据库地址无效。") from None
    shape = {
        "version": 1,
        "dialect": dialect,
        "view": settings.production_sql_view,
        "metrics": sorted(plan.metrics),
        "group_by": plan.group_by,
        "filter_fields": sorted(key for key, _ in plan.filters),
    }
    key = sha256(json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    identity = {"_id": key}
    projection = {"shape": 1, "enabled": 1}
    try:
        routes = mongo.db().production_sql_routes
        row = routes.find_one(identity, projection)
        if row is None:
            template = {
                "shape": shape,
                "enabled": True,
                "example_question": question,
                "sql": store.parameterized_sql(plan, view=settings.production_sql_view,
                                               url=settings.production_sql_url),
                "parameters": ["start_date", "end_date", "department_ids",
                               *(f"filter_{field}" for field in shape["filter_fields"])],
                "created_at": database.now_iso(),
            }
            # 并发请求只初始化一次。即使第一次查询期间管理员停用，也读回实际状态。
            try:
                row = routes.find_one_and_update(identity, {"$setOnInsert": template},
                                                 projection=projection, upsert=True,
                                                 return_document=ReturnDocument.AFTER)
            except DuplicateKeyError:
                row = routes.find_one(identity, projection)
    except PyMongoError:
        raise RouteUnavailable("无法读取生产查询路由，请稍后重试。") from None

    if not row or row.get("shape") != shape or type(row.get("enabled")) is not bool:
        raise RouteUnavailable("生产查询路由的配置不完整或与当前报表不一致。")
    if not row["enabled"]:
        raise RouteDisabled("管理员已停用此类生产查询。")
    saved = row["shape"]
    values = dict(plan.filters)
    bound = ProductionQuery(metrics=tuple(saved["metrics"]), group_by=saved["group_by"],
                            start=plan.start, end=plan.end,
                            filters=tuple((key, values[key]) for key in saved["filter_fields"]))
    return PreparedRoute(key, bound)


def record_execution(route_id: str, question: str, plan: ProductionQuery, username: str, outcome: str) -> None:
    """每次执行追加一条记录，次数和最后使用时间均从这份历史中计算。"""
    if outcome not in {"ok", "empty", "error"}:
        raise ValueError("生产查询执行状态无效。")
    parameters = {"start_date": plan.start.isoformat(), "end_date": plan.end.isoformat()}
    parameters.update({f"filter_{key}": value for key, value in plan.filters})
    try:
        mongo.db().production_sql_history.insert_one({
            "route_id": route_id, "question": question, "username": username, "outcome": outcome,
            "executed_at": database.now_iso(), "parameters": parameters,
        })
    except PyMongoError:
        raise RouteUnavailable("无法记录生产查询历史，请稍后重试。") from None


def list_routes(limit: int = 50, offset: int = 0) -> list[dict]:
    """返回当前页的模板，以及这些模板全部已记录执行的统计。"""
    fields = {"example_question": 1, "sql": 1, "parameters": 1, "enabled": 1}
    try:
        db = mongo.db()
        rows = list(db.production_sql_routes.find({}, fields).sort([("created_at", -1), ("_id", -1)])
                    .skip(max(0, offset)).limit(max(1, min(limit, 100))))
        if not rows:
            return []
        counts = db.production_sql_history.aggregate([
            {"$match": {"route_id": {"$in": [row["_id"] for row in rows]}}},
            {"$group": {"_id": "$route_id", "use_count": {"$sum": 1}, "last_used_at": {"$max": "$executed_at"}}},
        ])
        by_route = {entry["_id"]: entry for entry in counts}
        for row in rows:
            row["id"] = row.pop("_id")
            stats = by_route.get(row["id"], {})
            row["use_count"] = stats.get("use_count", 0)
            row["last_used_at"] = stats.get("last_used_at")
        return rows
    except PyMongoError:
        raise RouteUnavailable("无法读取生产查询路由，请稍后重试。") from None


def list_history(limit: int = 50, offset: int = 0) -> list[dict]:
    fields = {"_id": 0, "question": 1, "route_id": 1, "username": 1, "executed_at": 1,
              "outcome": 1, "parameters": 1}
    try:
        return list(mongo.db().production_sql_history.find({}, fields).sort("_id", -1)
                    .skip(max(0, offset)).limit(max(1, min(limit, 100))))
    except PyMongoError:
        raise RouteUnavailable("无法读取生产查询历史，请稍后重试。") from None


def set_enabled(route_id: str, enabled: bool) -> bool:
    if type(enabled) is not bool:
        raise ValueError("路由开关必须是布尔值。")
    try:
        update = mongo.db().production_sql_routes.update_one({"_id": route_id}, {"$set": {"enabled": enabled}})
        return update.matched_count == 1
    except PyMongoError:
        raise RouteUnavailable("无法修改生产查询路由，请稍后重试。") from None
