"""工作周报：按负责人汇总本周工作记录，定时生成，markdown 格式。

- 周报设置存 app_settings（weekly_report）：enabled / weekday(1=周一) / time / operators 名单。
- 本人计划存 weekly_report_settings，绑定账号代际标识，优先于统一计划。
- 周报存 weekly_reports 集合（_id = 周起始日:账号，重复生成幂等覆盖）。
- 正文以 LLM 小结为主：阅读本周全部记录原文后写总结，最后列出引用的文档（doc:// 可点击预览）；
  LLM 未配置或调用失败时回退为记录清单。
- 本人可生成自己的周报；管理员可生成名单内所有人。
- 调度：管理端启动一个守护线程，每分钟检查一次是否到达计划时间；到达且本周未生成时生成。
"""
from __future__ import annotations

import datetime
import json
import threading
import time
from uuid import uuid4

from app.core.llm import client as llm_client
from app.core import network_policy
from fastapi import HTTPException
from app.services import access, auth, database, kb, mongo

_SETTINGS_KEY = "weekly_report"
_RESOLVED_LABELS = {"resolved": "已完成", "improving": "处理中", "temporary": "阶段完成", "unresolved": "未完成"}
_CONTENT_LIMIT = 6000   # 单条记录正文截断长度，避免超出模型上下文
_SCHEDULER_LOCK = threading.Lock()


def get_settings() -> dict:
    """读取周报设置；缺省关闭、每周五 18:00、名单为空。"""
    raw = database.get_runtime_setting(_SETTINGS_KEY, "")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    weekday = data.get("weekday")
    operators = data.get("operators") or []
    return {
        "enabled": bool(data.get("enabled")),
        "weekday": weekday if isinstance(weekday, int) and 1 <= weekday <= 7 else 5,
        "time": str(data.get("time") or "18:00"),
        "operators": [str(o).strip() for o in operators if str(o).strip()],
    }


def save_settings(enabled: bool, weekday: int, time_text: str, operators: list[str]) -> dict:
    payload = _validate_schedule(enabled, weekday, time_text)
    payload["operators"] = sorted({str(o).strip() for o in operators or [] if str(o).strip()})
    database.set_runtime_setting(_SETTINGS_KEY, json.dumps(payload, ensure_ascii=False))
    return payload


def _validate_schedule(enabled: bool, weekday: int, time_text: str) -> dict:
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 1 <= weekday <= 7:
        raise ValueError("weekday 应为 1~7（1=周一）")
    if not isinstance(time_text, str) or len(time_text.split(":")) != 2:
        raise ValueError("时间格式应为 HH:MM")
    try:
        hh, mm = (int(part) for part in time_text.split(":"))
    except ValueError:
        raise ValueError("时间格式应为 HH:MM") from None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("时间格式应为 HH:MM")
    return {"enabled": bool(enabled), "weekday": weekday, "time": f"{hh:02d}:{mm:02d}"}


def _account_record(account: str, *, initialize: bool = False) -> dict | None:
    """账号标识在改密时保留，删除后重建时改变，不能用盐或秒级创建时间代替。"""
    users = mongo.db().users
    row = users.find_one({"_id": account})
    if row is not None and initialize and not row.get("account_id"):
        users.update_one({"_id": account, "account_id": row.get("account_id")},
                         {"$set": {"account_id": uuid4().hex}})
        row = users.find_one({"_id": account})
    return row


def _require_personal_account(account: str) -> dict:
    actor = access.current_identity() or {}
    if not access.is_internal() and (actor.get("public_only") or actor.get("must_change_password")
                                     or actor.get("username") != account):
        raise HTTPException(status_code=403, detail="只能设置本人的周报计划")
    row = _account_record(account, initialize=True)
    if row is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if actor.get("_account_id") and actor["_account_id"] != row.get("account_id"):
        raise HTTPException(status_code=409, detail="账号已变更，请重新登录后设置周报计划")
    return row


def _personal_payload(row: dict | None) -> dict:
    if row:
        try:
            return _validate_schedule(row.get("enabled", False), row.get("weekday", 5), row.get("time", "18:00"))
        except ValueError:
            pass
    return {"enabled": False, "weekday": 5, "time": "18:00"}


def get_personal_settings(account: str) -> dict:
    user = _require_personal_account(account)
    row = mongo.db().weekly_report_settings.find_one({"_id": account, "account_id": user["account_id"]})
    return _personal_payload(row)


def save_personal_settings(account: str, enabled: bool, weekday: int, time_text: str) -> dict:
    payload = _validate_schedule(enabled, weekday, time_text)
    user = _require_personal_account(account)
    plans = mongo.db().weekly_report_settings
    original = plans.find_one({"_id": account})
    update = {"$set": {**payload, "account_id": user["account_id"], "updated_at": database.now_iso()}}
    if not original or original.get("account_id") != user["account_id"]:
        update["$unset"] = {"last_generated_week": ""}
    plans.update_one({"_id": account}, update, upsert=True)
    return payload


def week_start(day: datetime.date | None = None) -> str:
    """所在周的周一（ISO 日期），周报的统计起点。"""
    d = day or datetime.date.today()
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def collect_records(week_start: str, person: str) -> list[dict]:
    """某人本周（week_start 起 7 天内）的工作记录，按时间倒序；标题完整保留。"""
    end = (datetime.date.fromisoformat(week_start) + datetime.timedelta(days=7)).isoformat()
    items = []
    for doc in kb._visible_records():
        if doc.doc_type != "record":
            continue
        owner = (doc.uploaded_by or "").strip()
        if owner != person:
            continue
        day = (doc.uploaded_at or "")[:10]
        if not (week_start <= day < end):
            continue
        items.append({"doc_id": doc.id, "doc_name": (doc.name or "").strip(), "date": day,
                      "resolved": _RESOLVED_LABELS.get(doc.resolved, "")})
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


def record_owners(*, managed_only: bool = False) -> list[str]:
    """选择周报对象使用不可变账号，显示名仅用于页面展示。"""
    departments = access.managed_departments()
    return sorted({doc.uploaded_by for doc in kb._visible_records()
                   if doc.doc_type == "record" and doc.uploaded_by
                   and (not managed_only or doc.department_id in departments)})


def _can_generate_for(account: str) -> bool:
    if access.is_platform_admin():
        return True
    actor = access.current_identity() or {}
    if account == actor.get("username"):
        return True
    row = mongo.db().users.find_one({"_id": account})
    return bool(row and access.managed_departments().intersection(row.get("department_ids", [])))


def visible_reports(limit: int = 12) -> list[dict]:
    """按稳定账号读取，并在返回缓存摘要前重新检查全部来源文档。"""
    query = {} if access.is_platform_admin() else {"owner_account": {"$exists": True}}
    result = []
    for row in mongo.db().weekly_reports.find(query, {"_id": 0}).sort("week_start", -1):
        account = row.get("owner_account", "")
        if not access.is_platform_admin() and (not account or not _can_generate_for(account)):
            continue
        if row.get("owner_account_id"):
            user = _account_record(account)
            if user is None or user.get("account_id") != row["owner_account_id"]:
                continue
        items = row.get("items", [])
        if any((doc := database.get(item.get("doc_id", ""))) is None or not access.can_read(doc)
               for item in items):
            continue  # 摘要可能包含已撤权来源，不能只删除来源列表再继续返回摘要
        if row.get("access_revision") != database.access_revision():
            continue
        result.append(row)
        if len(result) >= limit:
            break
    return result


_WEEKLY_PROMPT = """你是企业各部门的工作周报撰写助手。请阅读某位负责人本周的全部工作记录原文，写一段周报小结。

要求：
1. 概括本周主要完成了哪些工作、取得了什么结果、还有什么待办；按「事项 → 处理与结果」组织，不要逐条罗列标题。
2. 只使用记录原文中的工作事项、处理步骤与结果，不要推测或补充记录里没有的内容。
3. 中文，250~400 字，直接输出小结正文；不要标题、不要客套话、不要列表。"""


def _record_content(doc_id: str) -> str:
    """读取记录原文（Markdown 存档）；读取失败返回空串。"""
    try:
        path, _name = kb.source_file(doc_id)
        return path.read_text(encoding="utf-8", errors="replace").strip()[:_CONTENT_LIMIT]
    except Exception:   # noqa: BLE001  记录缺失或为旧导入文件时不阻断周报生成
        return ""


def summarize(person: str, week: str, items: list[dict]) -> str:
    """LLM 阅读本周全部记录正文后的小结；无记录或调用失败返回空串（由清单兜底）。"""
    if not network_policy.external_apis_enabled():
        return ""
    entries = []
    for it in items:
        content = _record_content(it.get("doc_id", ""))
        if not content:
            continue
        label = it.get("doc_name") or "未命名"
        status = it.get("resolved") or "未标记"
        entries.append(f"【{label}】（{it.get('date', '')}，{status}）\n{content}")
    if not entries:
        return ""
    user = f"负责人：{person}（{week} 那周，共 {len(entries)} 条记录）\n\n" + "\n\n".join(entries)
    try:
        return llm_client.chat(_WEEKLY_PROMPT, user).strip()
    except Exception:   # noqa: BLE001  LLM 未配置或失败不阻断周报生成
        return ""


def render_markdown(week: str, person: str, items: list[dict], summary: str = "") -> str:
    """周报 markdown：正文以 LLM 小结为主，最后列出引用文档（doc:// 可点击预览）。"""
    end = (datetime.date.fromisoformat(week) + datetime.timedelta(days=6)).isoformat()
    lines = [f"# {person} 的工作周报（{week} ~ {end}）", ""]
    if not items:
        return "\n".join([*lines, "本周没有工作记录。", ""]) + "\n"
    lines.append(summary.strip() or "（本周小结生成失败，以下为本周记录清单。）")
    lines += ["", "## 本周工作记录"]
    for it in items:
        name = it.get("doc_name") or "未命名记录"
        tail = f"（{it.get('date', '')}）{it.get('resolved') or ''}".strip()
        lines.append(f"- [{name}](doc://{it.get('doc_id', '')}){tail}")
    return "\n".join(lines) + "\n"


def generate_reports(week: str | None = None, operators: list[str] | None = None) -> list[dict]:
    """为名单中的每个账号生成（或覆盖）本周周报；返回生成结果。"""
    week = week or week_start()
    targets = [o.strip() for o in (operators if operators is not None else get_settings()["operators"]) if o.strip()]
    generated_at = database.now_iso()
    results = []
    for person in targets:
        if not _can_generate_for(person):
            raise HTTPException(status_code=403, detail="没有此账号的周报管理权限")
        revision = database.access_revision()
        user = _account_record(person, initialize=True)
        owner_id = (user or {}).get("account_id", "")
        expected_owner = (access.current_identity() or {}).get("_account_id")
        if expected_owner and person == (access.current_identity() or {}).get("username") and owner_id != expected_owner:
            raise HTTPException(status_code=409, detail="账号已变更，请重新登录后生成周报")
        items = collect_records(week, person)
        summary = summarize(person, week, items)
        markdown = render_markdown(week, person, items, summary)
        _check_generation_permissions(person, owner_id, revision, items)
        database.save_weekly_report(week, person, items, generated_at, markdown=markdown,
                                    permission_revision=revision, owner_account_id=owner_id)
        _check_generation_permissions(person, owner_id, revision, items)
        results.append({"week_start": week, "person": person, "items": items,
                        "markdown": markdown, "summary": summary or "",
                        "generated_at": generated_at})
    return results


def _check_generation_permissions(person: str, owner_id: str, revision: int, items: list[dict]) -> None:
    user = _account_record(person)
    if (database.access_revision() != revision or (user or {}).get("account_id", "") != owner_id
            or any((doc := database.get(item["doc_id"])) is None or not access.can_read(doc) for item in items)):
        raise HTTPException(status_code=409, detail="权限已变更，请重新生成周报")


def _schedule_due(conf: dict, now: datetime.datetime) -> bool:
    if not conf.get("enabled"):
        return False
    try:
        conf = _validate_schedule(True, conf["weekday"], conf["time"])
    except (KeyError, ValueError):
        return False
    day = datetime.date.fromisoformat(week_start(now.date())) + datetime.timedelta(days=conf["weekday"] - 1)
    hour, minute = (int(part) for part in conf["time"].split(":"))
    planned = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=now.tzinfo)
    return now >= planned


def _personal_schedules() -> dict[str, tuple[dict, dict]]:
    """只取当前账号代际的计划，旧账号同名重建不会继承旧设置或覆盖统一计划。"""
    schedules = {}
    for plan in mongo.db().weekly_report_settings.find({"account_id": {"$type": "string", "$ne": ""}}):
        user = _account_record(plan["_id"])
        if user is not None and user.get("account_id") == plan["account_id"]:
            schedules[plan["_id"]] = (plan, user)
    return schedules


def run_scheduled_once(now: datetime.datetime | None = None) -> bool:
    """按本人优先的计划生成；同周已有结果时不重复调用模型或覆盖手动结果。"""
    now = now or datetime.datetime.now()
    week = week_start(now.date())
    generated = False
    with _SCHEDULER_LOCK:
        personal = _personal_schedules()
        for account, (plan, user) in personal.items():
            if not _schedule_due(plan, now) or plan.get("last_generated_week") == week:
                continue
            identity = auth._row_identity(user)
            if identity.get("must_change_password"):
                continue
            identity["_account_id"] = plan["account_id"]
            cached = mongo.db().weekly_reports.find_one({"_id": f"{week}:{account}"})
            if (cached and cached.get("owner_account_id") == plan["account_id"]
                    and cached.get("access_revision") == database.access_revision()):
                continue
            try:
                with access.identity_scope(identity):
                    reports = generate_reports(week, operators=[account])
                if reports:
                    mongo.db().weekly_report_settings.update_one(
                        {"_id": account, "account_id": plan["account_id"]},
                        {"$set": {"last_generated_week": week}})
                    generated = True
            except Exception:   # noqa: BLE001 单个账号失败不阻断其他人的计划，下轮可重试。
                continue
        conf = get_settings()
        if _schedule_due(conf, now):
            targets = []
            for account in conf["operators"]:
                if account in personal or database.has_weekly_report(week, account):
                    continue
                user = _account_record(account)
                if user is not None and not auth._row_identity(user).get("must_change_password"):
                    targets.append(account)
            if targets:
                generated = bool(generate_reports(week, operators=targets)) or generated
    return generated


def start_weekly_scheduler(interval_sec: int = 60) -> threading.Thread:
    """管理端启动的守护线程：周期检查计划时间并生成本周周报。"""

    def loop() -> None:
        while True:
            try:
                run_scheduled_once()
            except Exception:   # noqa: BLE001  调度失败不中断服务，下轮重试
                pass
            time.sleep(interval_sec)

    thread = threading.Thread(target=loop, name="weekly-report-scheduler", daemon=True)
    thread.start()
    return thread
