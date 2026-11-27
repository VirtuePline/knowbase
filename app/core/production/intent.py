"""把生产统计问题转换成有限的指标、日期和等值筛选条件。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


@dataclass(frozen=True)
class ProductionQuery:
    metrics: tuple[str, ...]
    start: date
    end: date  # 不含结束日
    group_by: str | None = None
    filters: tuple[tuple[str, str], ...] = ()


_METRICS = re.compile(
    r"(?P<yield_rate>(?<!不)(?:良品率|良率|合格率))"
    r"|(?P<defect>(?:不良品|不合格品|次品|废品|不良)(?:数量|件数|总数|数)?)"
    r"|(?P<good>(?:良品|合格品)(?:数量|件数|总数|数)?)"
    r"|(?P<order_count>(?:生产)?(?:订单|工单)(?:数量|总数|数目|数|(?:有)?多少)|"
    r"(?:多少|几)(?:个|张|笔)?(?:生产)?(?:订单|工单))"
    r"|(?P<output>(?:总产量|产量|生产数量|报工数量|生产总数|完工数量)|"
    r"(?:生产|产出|完工|产)了?(?:多少|几)(?:件|台|套|个|箱|批)?)"
)
_DOMAIN = re.compile(r"生产(?:数据|统计|线|日报|记录|工单|订单)|报工|产量|产出|良品|良率|次品|废品")
_KNOWLEDGE = re.compile(
    r"(?:如何|怎样|怎么)(?!样)[^\s?!。！？,，]+"
    r"|什么是|是什么(?:意思)?|定义|公式|计算方法|口径|说明书|流程|规定|规范|制度|教程"
    r"|为什么|原因|因素|报错|出错|失败|故障|异常|不一致|对不上|不对|日志"
)
_MUTATION = re.compile(r"删除|清空|清除|修改|更改|更新为|改为|改成|写入|插入|覆盖|重置")
_OTHER_UNIT = re.compile(r"(?:多少|几|按|每|折合|换算成|单位[为是]?)\s*[箱批台套]")
_SQL = re.compile(r"\b(?:select\b.*?\bfrom|update\b.*?\bset|delete\s+from|insert\s+into|drop\s+table|alter\s+table)\b", re.I | re.S)
_DATE = r"\d{4}(?:[-/.]\d{1,2}[-/.]\d{1,2}|年\d{1,2}月\d{1,2}日?)"
_PERIOD = re.compile(
    rf"(?P<first>{_DATE})\s*(?:(?:至|到|~|—|–|-)\s*(?P<last>{_DATE}))?"
    r"|(?:最近|近)\s*(?P<days>\d+)\s*天"
    r"|前天|昨天|昨日|今天|今日|上个?月|本月|这个月|当月|上周|本周|这周"
)
_DIMENSIONS = {"产线": "line", "生产线": "line", "产品": "product", "产品编码": "product",
               "产品编号": "product", "班次": "shift", "日期": "day", "天": "day", "日": "day"}
_GROUP = re.compile(r"(?:按|各个?|每个?|不同|分)\s*(生产线|产线|产品编码|产品编号|产品|班次|日期|天|日)(?:分组)?")
_LABELS = {"生产线": "line", "产线": "line", "产品编码": "product", "产品编号": "product",
           "产品": "product", "班次": "shift", "生产工单": "order_id", "工单号": "order_id", "工单": "order_id"}
_FILTER = re.compile(
    r'(生产线|产线|产品编码|产品编号|产品|班次|生产工单|工单号|工单)\s*(?:为|是|=|:)?\s*'
    r'(?:"([^"\n]{1,64})"|“([^”\n]{1,64})”|([A-Za-z0-9][A-Za-z0-9_./-]{0,63}(?:号线|线|班)?)'
    r'|([早中晚夜白]班|[一二三四五六七八九十]+号线))'
)
_GROUP_OR_FILTER = re.compile(f"(?:{_GROUP.pattern})|(?:{_FILTER.pattern})")
_BARE_FILTER = re.compile(r"(?P<line>[A-Za-z0-9一二三四五六七八九十]+\s*(?:号\s*)?线)|(?P<shift>[早中晚夜白]班)")
_FILLER = re.compile(
    r"帮我|请问|请|麻烦|查询|查一下|查下|查|看一下|看下|看看|统计一下|统计|汇总|显示|列出|告诉我"
    r"|分别|一共|合计|总共|总计|是多少|有多少|多少|几件|怎么样|如何|情况|从|期间|之间|的|了|和|与|及|有|共|各|个"
    r"|[\s,、。?!]+"
)
_CLARIFY_METRIC = "请指定要查的生产指标：产量、良品数、不良品数、良率或工单数。"
_CLARIFY_PERIOD = "请给出不超过 31 天的统计日期，例如“昨天的产量”或“2026-11-01 至 2026-11-07 的良品数”。"
_CLARIFY_FILTER = ('请明确统计条件。可按产线、产品、班次或日期中的一项分组，'
                   '或用 产线=A线、产品=P01、班次=夜班、工单=WO01 等值筛选。')


def _calendar_date(value: str) -> date:
    year, month, day = map(int, re.findall(r"\d+", value))
    return date(year, month, day)


def _bounds(period: re.Match[str], today: date) -> tuple[date, date]:
    day = timedelta(days=1)
    if period["first"]:
        start = _calendar_date(period["first"])
        end = _calendar_date(period["last"]) if period["last"] else start
        return start, end + day
    if period["days"]:
        return today - day * (int(period["days"]) - 1), today + day
    label = period[0]
    if "月" in label:
        start = today.replace(day=1)
        if label.startswith("上"):
            return (start - day).replace(day=1), start
        return start, today + day
    if "周" in label:
        monday = today - day * today.weekday()
        return (monday - day * 7, monday) if label == "上周" else (monday, today + day)
    start = today - day * {"前天": 2, "昨天": 1, "昨日": 1, "今天": 0, "今日": 0}[label]
    return start, start + day


def parse_production_question(question: str, today: date | None = None) -> ProductionQuery | str | None:
    """None 交给知识库，字符串作澄清提示。只有完整解析的结构化查询可以送数据库。"""
    text = unicodedata.normalize("NFKC", question).strip()
    groups: set[str] = set()
    filters: list[tuple[str, str]] = []

    def take_condition(m: re.Match[str]) -> str:
        if group := _GROUP.fullmatch(m[0]):
            groups.add(_DIMENSIONS[group[1]])
        else:
            field = _FILTER.fullmatch(m[0])
            value = next(v for v in field.groups()[1:] if v)
            filters.append((_LABELS[field[1]], value.strip()))
        return " "

    remaining = _GROUP_OR_FILTER.sub(take_condition, text)
    metrics = tuple(dict.fromkeys(match.lastgroup for match in _METRICS.finditer(remaining)))
    if not metrics and not _DOMAIN.search(remaining) and "production_daily" not in remaining.lower():
        return None
    if _KNOWLEDGE.search(remaining):
        return None
    if _MUTATION.search(remaining) or _SQL.search(text):
        return "只支持按生产指标进行只读统计。请用自然语言描述日期、指标和筛选条件。"
    if not metrics:
        return _CLARIFY_METRIC
    if _OTHER_UNIT.search(remaining):
        return "生产报表按件记录数量。请按件提问，例如“昨天生产了多少件？”"
    periods = list(_PERIOD.finditer(remaining))
    if len(periods) != 1:
        return _CLARIFY_PERIOD
    try:
        start, end = _bounds(periods[0], today or datetime.now(timezone(timedelta(hours=8))).date())
    except (ValueError, OverflowError):
        return _CLARIFY_PERIOD
    if not 0 < (end - start).days <= 31:
        return _CLARIFY_PERIOD

    if len(groups) > 1:
        return _CLARIFY_FILTER
    remaining = _METRICS.sub(" ", _PERIOD.sub(" ", remaining))
    filters.extend((m.lastgroup, "".join(m[0].split())) for m in _BARE_FILTER.finditer(remaining))
    remaining = _BARE_FILTER.sub(" ", remaining)
    # 未识别的限定词可能是车间、部门或其他业务范围，不能悄悄丢掉再查全量。
    if _FILLER.sub("", remaining) or len(dict(filters)) != len(filters) or any(not v for _, v in filters):
        return _CLARIFY_FILTER
    return ProductionQuery(metrics, start, end, next(iter(groups), None), tuple(filters))
