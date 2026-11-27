"""从预先配置的生产报表视图读取汇总值。视图的一行代表一笔可累加的生产记录。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from sqlalchemy import BigInteger, Date, String, bindparam, case, cast, column, create_engine, func, select, table
from sqlalchemy.dialects import mssql, mysql, postgresql, sqlite
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.core.production.intent import ProductionQuery


class ProductionStoreError(Exception):
    """可安全显示给用户的配置或查询错误。"""


@dataclass(frozen=True)
class ProductionResult:
    rows: tuple[dict, ...]
    truncated: bool = False


_QUANTITIES = {"output": "output_qty", "good": "good_qty", "defect": "defect_qty"}
_DIMENSIONS = {"line": "line_name", "product": "product_code", "shift": "shift_name", "order_id": "order_no"}
_METRICS = frozenset(_QUANTITIES) | {"order_count", "yield_rate"}
_GROUPS = {"day": "production_date", **{key: _DIMENSIONS[key] for key in ("line", "product", "shift")}}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_MAX_GROUPS = 100
_DIALECTS = {"mysql": mysql, "sqlite": sqlite, "postgresql": postgresql, "mssql": mssql}


def _report_view(name: str):
    parts = name.split(".")
    if len(parts) > 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise ProductionStoreError("生产报表视图名称配置无效。")
    return table(
        parts[-1], column("production_date", Date), column("department_id", String),
        *(column(field, String) for field in _DIMENSIONS.values()),
        *(column(field, BigInteger) for field in _QUANTITIES.values()),
        schema=parts[0] if len(parts) == 2 else None,
    )


@lru_cache(maxsize=4)
def _engine(url: str) -> Engine:
    """只在实际查询时加载驱动。查询结束就关闭连接，不保留生产库会话。"""
    parsed = make_url(url)
    dialect = parsed.drivername
    args = {}
    if dialect in {"sqlite", "sqlite+pysqlite"}:
        # 使用 SQLite 自己的只读打开模式，检查与连接之间文件被删除也不会创建空库。
        if not parsed.database or parsed.database == ":memory:" or parsed.query:
            raise ProductionStoreError("请为生产报表配置已存在的 SQLite 文件路径。")
        path = Path(parsed.database).resolve(strict=True)
        if not path.is_file():
            raise ProductionStoreError("生产报表数据库文件不可用。")
        parsed = URL.create("sqlite+pysqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"})
        args = {"timeout": 5}
    elif dialect in {"postgresql", "postgresql+psycopg", "postgresql+psycopg2"}:
        options = parsed.query.get("options", "")
        args = {"connect_timeout": 5,
                "options": f"{options} -c default_transaction_read_only=on -c statement_timeout=15000"}
    elif dialect in {"mssql", "mssql+pyodbc"}:
        args = {"timeout": 5}
    elif dialect == "mysql+pymysql":
        args = {"connect_timeout": 5, "read_timeout": 15, "write_timeout": 5}
    else:
        raise ProductionStoreError("生产报表的数据库驱动尚不支持。")
    if "plugin" in parsed.query:
        raise ProductionStoreError("生产报表的连接参数无效。")
    return create_engine(parsed, poolclass=NullPool, hide_parameters=True, connect_args=args)


def _validated_statement(plan: ProductionQuery, view: str, dialect: str):
    if (type(plan.start) is not date or type(plan.end) is not date
            or not 1 <= (plan.end - plan.start).days <= 31):
        raise ProductionStoreError("生产指标的查询区间须为 1 至 31 天。")
    if (not plan.metrics or len(set(plan.metrics)) != len(plan.metrics)
            or not set(plan.metrics).issubset(_METRICS)
            or plan.group_by is not None and plan.group_by not in _GROUPS):
        raise ProductionStoreError("不支持所选的生产指标或分组。")

    keys = []
    for key, value in plan.filters:
        if key not in _DIMENSIONS or key in keys or not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
            raise ProductionStoreError("生产查询的筛选条件无效。")
        keys.append(key)
    return _statement(view, dialect, tuple(sorted(plan.metrics)), plan.group_by, tuple(sorted(keys)))


@lru_cache(maxsize=256)
def _statement(view: str, dialect: str, metrics: tuple[str, ...], group_by: str | None, keys: tuple[str, ...]):
    # 缓存中只保存报表结构；每次实际查询时重新绑定日期、部门和维度筛选值。
    report = _report_view(view)
    scope = bindparam("department_ids", expanding=True, type_=String)
    department = report.c.department_id
    conditions = [report.c.production_date >= bindparam("start_date", type_=Date),
                  report.c.production_date < bindparam("end_date", type_=Date),
                  department.in_(scope)]
    # 普通 IN 可利用源库索引。再精确比对部门 ID，避免源库默认排序规则将不同 ID 判为相同。
    if dialect == "mysql":
        exact_department = cast(department, mysql.BINARY)
    elif dialect == "mssql":
        exact_department = department.collate("Latin1_General_100_BIN2")
    elif dialect == "postgresql":
        exact_department = cast(department, String).collate("C")
    else:
        exact_department = department.collate("BINARY")
    conditions.append(exact_department.in_(scope))
    conditions.extend(report.c[_DIMENSIONS[key]] == bindparam(f"filter_{key}", type_=String) for key in keys)
    count = func.count()
    columns = [count.label("_records")]
    required = set(metrics)
    if "yield_rate" in required:
        required.update(("output", "good"))
    for metric, field in _QUANTITIES.items():
        if metric in required:
            qty = report.c[field]
            # 部分记录未填数量时保留未知，避免 SQL SUM 忽略 NULL 后显示成完整产量。
            columns.append(case((func.count(qty) == count, func.sum(cast(qty, BigInteger)))).label(metric))
    if "order_count" in required:
        order_no = func.nullif(func.trim(report.c.order_no), "")
        columns.append(case((func.count(order_no) == count, func.count(order_no.distinct()))).label("order_count"))

    statement = select(*columns).select_from(report).where(*conditions)
    if dialect == "mysql":
        statement = statement.prefix_with("/*+ MAX_EXECUTION_TIME(15000) */", dialect="mysql")
    if group_by:
        group = report.c[_GROUPS[group_by]]
        statement = (statement.add_columns(group.label(group_by))
                     .group_by(group).order_by(group).limit(_MAX_GROUPS + 1))
    return statement


def parameterized_sql(plan: ProductionQuery, *, view: str, url: str) -> str:
    """生成供模板记录的 SQL 展示文本。部门列表在实际执行时按本次授权展开。"""
    try:
        dialect = make_url(url).get_backend_name()
    except (SQLAlchemyError, ValueError):
        raise ProductionStoreError("生产报表的数据库地址无效。") from None
    if dialect not in _DIALECTS:
        raise ProductionStoreError("生产报表的数据库驱动尚不支持。")
    statement = _validated_statement(plan, view, dialect)
    compiled = statement.compile(dialect=_DIALECTS[dialect].dialect(paramstyle="named"))
    sql = str(compiled).replace("__[POSTCOMPILE_department_ids]", ":department_ids")
    # 常量来自上面构造的 SELECT。显示分组上限及空字符串，只保留真正由每次查询提供的占位符。
    for key, value in compiled.params.items():
        if type(value) is int or value == "":
            token = rf"__\[POSTCOMPILE_{re.escape(key)}\]|:{re.escape(key)}\b"
            sql = re.sub(token, str(value) if type(value) is int else "''", sql)
    return sql


def query_production(plan: ProductionQuery, *, department_ids: set[str], url: str, view: str) -> ProductionResult:
    """日期右端不含。空结果与数量为零有不同含义，良品率按总良品量/总产量返回百分数。"""
    if (not isinstance(department_ids, (set, frozenset)) or not department_ids
            or any(not isinstance(item, str) or not item.strip() for item in department_ids)):
        raise ProductionStoreError("当前账号没有可查询的生产部门。")
    if not url:
        raise ProductionStoreError("生产报表数据源尚未配置。")
    try:
        engine = _engine(url)
        statement = _validated_statement(plan, view, engine.dialect.name)
        params = {"start_date": plan.start, "end_date": plan.end, "department_ids": sorted(department_ids)}
        params.update({f"filter_{key}": value for key, value in plan.filters})
        with engine.connect() as connection:
            if engine.dialect.name == "mssql":
                connection.connection.driver_connection.timeout = 15
            values = connection.execute(statement, params).mappings().all()
    except (SQLAlchemyError, ImportError, OSError, ValueError):
        # 驱动消息有时附带 URL、参数或实际 SQL，勿将异常原文透传给问答接口。
        raise ProductionStoreError("生产报表读取失败，请检查数据源连接、读取权限和报表视图。") from None

    rows = []
    for row in values[:_MAX_GROUPS]:
        if not row["_records"]:
            continue
        result = {}
        if plan.group_by:
            group = row[plan.group_by]
            result[plan.group_by] = group.isoformat() if isinstance(group, date) else group
        for metric in plan.metrics:
            if metric == "yield_rate":
                good, output = row["good"], row["output"]
                result[metric] = (round(Decimal(good) * 100 / Decimal(output), 2)
                                  if good is not None and output else None)
            else:
                result[metric] = int(row[metric]) if row[metric] is not None else None
        rows.append(result)
    return ProductionResult(tuple(rows), truncated=len(values) > _MAX_GROUPS)
