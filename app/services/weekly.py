"""运维周报：按运维人汇总本周运维记录，定时生成，每件事压缩到 20 字以内。

- 周报设置存 app_settings（weekly_report）：enabled / weekday(1=周一) / time / operators 名单。
- 周报存 weekly_reports 集合（_id = 周起始日:运维人，重复生成幂等覆盖）。
- 调度：管理端启动一个守护线程，每分钟检查一次是否到达计划时间；到达且本周未生成时生成。
"""
from __future__ import annotations

import datetime
import json
import threading
import time

from app.services import database

_SETTINGS_KEY = "weekly_report"
_ITEM_MAX_CHARS = 20   # 每件事最多保留的字数


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
    if not isinstance(weekday, int) or not 1 <= weekday <= 7:
        raise ValueError("weekday 应为 1~7（1=周一）")
    if not time_text or len(time_text.split(":")) != 2:
        raise ValueError("时间格式应为 HH:MM")
    hh, mm = (int(part) for part in time_text.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("时间格式应为 HH:MM")
    payload = {
        "enabled": bool(enabled),
        "weekday": weekday,
        "time": f"{hh:02d}:{mm:02d}",
        "operators": sorted({str(o).strip() for o in operators or [] if str(o).strip()}),
    }
    database.set_runtime_setting(_SETTINGS_KEY, json.dumps(payload, ensure_ascii=False))
    return payload


def week_start(day: datetime.date | None = None) -> str:
    """所在周的周一（ISO 日期），周报的统计起点。"""
    d = day or datetime.date.today()
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def collect_records(week_start: str, person: str) -> list[dict]:
    """某人本周（week_start 起 7 天内）的运维记录，按时间倒序。"""
    end = (datetime.date.fromisoformat(week_start) + datetime.timedelta(days=7)).isoformat()
    items = []
    for doc in database.list_all():
        if doc.doc_type != "record":
            continue
        owner = (doc.uploaded_by_name or doc.uploaded_by or "").strip()
        if owner != person:
            continue
        day = (doc.uploaded_at or "")[:10]
        if not (week_start <= day < end):
            continue
        items.append({"date": day, "text": (doc.name or "").strip()[:_ITEM_MAX_CHARS],
                      "doc_id": doc.id})
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


def generate_reports(week: str | None = None, operators: list[str] | None = None) -> list[dict]:
    """为名单中的每个运维人生成（或覆盖）本周周报；返回生成结果。"""
    week = week or week_start()
    targets = [o.strip() for o in (operators if operators is not None else get_settings()["operators"]) if o.strip()]
    generated_at = database.now_iso()
    results = []
    for person in targets:
        items = collect_records(week, person)
        database.save_weekly_report(week, person, items, generated_at)
        results.append({"week_start": week, "person": person, "items": items,
                        "generated_at": generated_at})
    return results


def run_scheduled_once(now: datetime.datetime | None = None) -> bool:
    """到达计划时间且本周名单尚未生成时执行生成；返回是否实际生成。"""
    conf = get_settings()
    if not conf["enabled"] or not conf["operators"]:
        return False
    now = now or datetime.datetime.now()
    planned = now.replace(hour=int(conf["time"][:2]), minute=int(conf["time"][3:5]),
                          second=0, microsecond=0)
    if now < planned:
        return False
    week = week_start(now.date())
    # 今天还没到计划星期：新一周尚未到点，不生成
    if now.weekday() + 1 < conf["weekday"]:
        return False
    return bool(generate_reports(week))


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
