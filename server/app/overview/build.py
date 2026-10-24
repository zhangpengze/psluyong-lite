"""概览数据聚合：KPI / 趋势 / 合作伙伴明细（含各模板用量）。

为什么直接聚合、不建预聚合表：
    这是单主管内网看板，records 按天增长几十条量级。带
    (partner_name, work_date) 复合索引的 GROUP BY 在几千行规模是毫秒级；
    再叠一张 nightly 聚合表，反而要处理软删除/恢复后的重修一致性——
    用复杂度换一个不存在的性能问题。升级路径写在
    docs/design/dashboard-redesign.md：records 过 10 万行、或本接口
    P95 超过 500ms 再上预聚合，接口形状不用变。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import distinct, func

from ..config import Config
from ..db import db
from ..models import STATUS_FAILED, Record
from ..status import system_status

# 伙伴活跃度口径：按"今天 - 最近一次调用"的天数分档
ACTIVE_MAX_DAYS = 7
WARNING_MAX_DAYS = 14
STATUS_LABELS = {"active": "活跃", "warning": "预警", "dormant": "停用"}

CALLS_DEFINITION = "is_deleted=0 的全部记录（含失败）"


def _display_tz():
    try:
        return ZoneInfo(Config.TZ)
    except Exception:  # noqa: BLE001 - 缺 tzdata 时退回服务器本地时间
        return None


def _totals(start: date, end: date) -> tuple[int, int]:
    """区间内的 (总记录数, 失败数)"""
    base = db.session.query(func.count(Record.id)).filter(
        Record.is_deleted.is_(False),
        Record.work_date >= start,
        Record.work_date <= end,
    )
    total = base.scalar() or 0
    failed = base.filter(Record.status == STATUS_FAILED).scalar() or 0
    return total, failed


def _activity_status(last_active: date | None, today: date) -> str:
    if last_active is None:
        return "dormant"
    gap = (today - last_active).days
    if gap <= ACTIVE_MAX_DAYS:
        return "active"
    if gap <= WARNING_MAX_DAYS:
        return "warning"
    return "dormant"


def build_overview(start: date, end: date, label: str, preset: str) -> dict:
    today = date.today()
    total, failed = _totals(start, end)

    # 环比参照：长度相同、紧邻的前一个区间
    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_total, _prev_failed = _totals(prev_end - timedelta(days=days - 1), prev_end)

    # 趋势：按天补零，中间没数据的日子也要占位，图才连续
    trend_rows = (
        db.session.query(Record.work_date, func.count(Record.id))
        .filter(
            Record.is_deleted.is_(False),
            Record.work_date >= start,
            Record.work_date <= end,
        )
        .group_by(Record.work_date)
        .all()
    )
    counted = {d: c for d, c in trend_rows}
    series = []
    cursor = start
    while cursor <= end:
        series.append({"date": cursor.isoformat(), "count": counted.get(cursor, 0)})
        cursor += timedelta(days=1)
    peak = max(series, key=lambda p: p["count"])

    # 合作伙伴 × 模板：一次分组拿齐两级明细。
    # 空伙伴名不进合作伙伴维度（员工端没填），但记录本身照常计入上面的总量。
    pair_rows = (
        db.session.query(
            Record.partner_name,
            Record.template_name,
            func.count(Record.id),
            func.max(Record.work_date),
        )
        .filter(
            Record.is_deleted.is_(False),
            Record.work_date >= start,
            Record.work_date <= end,
            Record.partner_name.isnot(None),
            Record.partner_name != "",
        )
        .group_by(Record.partner_name, Record.template_name)
        .all()
    )

    partners: dict[str, dict] = {}
    for name, template, count, last_active in pair_rows:
        partner = partners.setdefault(
            name,
            {"name": name, "calls": 0, "templates": [], "last_active": None},
        )
        partner["calls"] += count
        partner["templates"].append({"name": template or "(未填模板名)", "calls": count})
        if last_active and (partner["last_active"] is None or last_active > partner["last_active"]):
            partner["last_active"] = last_active

    # 伙伴首次出现日（全历史）：用于"区间新增"
    first_seen = {
        name: seen
        for name, seen in db.session.query(Record.partner_name, func.min(Record.work_date))
        .filter(
            Record.is_deleted.is_(False),
            Record.partner_name.isnot(None),
            Record.partner_name != "",
        )
        .group_by(Record.partner_name)
        .all()
    }

    employees_active = (
        db.session.query(func.count(distinct(Record.employee_name)))
        .filter(
            Record.is_deleted.is_(False),
            Record.work_date >= start,
            Record.work_date <= end,
        )
        .scalar()
        or 0
    )
    templates_active = (
        db.session.query(func.count(distinct(Record.template_name)))
        .filter(
            Record.is_deleted.is_(False),
            Record.work_date >= start,
            Record.work_date <= end,
        )
        .scalar()
        or 0
    )

    partner_list = []
    for partner in partners.values():
        calls = partner["calls"]
        status = _activity_status(partner["last_active"], today)
        templates = sorted(partner["templates"], key=lambda t: (-t["calls"], t["name"]))
        for tpl in templates:
            tpl["share"] = round(tpl["calls"] / calls * 100, 1) if calls else 0.0
        seen = first_seen.get(partner["name"])
        partner_list.append(
            {
                "name": partner["name"],
                "calls": calls,
                "templates_used": len(templates),
                "templates": templates,
                "last_active": partner["last_active"].isoformat() if partner["last_active"] else None,
                "status": status,
                "status_label": STATUS_LABELS[status],
                "share": round(calls / total * 100, 1) if total else 0.0,
                "first_seen": seen.isoformat() if seen else None,
            }
        )
    partner_list.sort(key=lambda p: (-p["calls"], p["name"]))

    new_partners = sum(1 for seen in first_seen.values() if start <= seen <= end)
    tz = _display_tz()
    now = datetime.now(tz) if tz else datetime.now()

    return {
        "ok": True,
        "range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": label,
            "preset": preset,
            "days": days,
        },
        "updated_at": now.strftime("%H:%M:%S"),
        "updated_iso": now.isoformat(),
        "kpi": {
            "calls": total,
            "calls_prev": prev_total,
            "calls_delta_pct": (
                round((total - prev_total) / prev_total * 100, 1) if prev_total else None
            ),
            "success": total - failed,
            "failed": failed,
            "success_rate": round((total - failed) / total * 100, 1) if total else 0.0,
            "partners_active": len(partner_list),
            "partners_total": len(first_seen),
            "partners_new": new_partners,
            "partners_inactive": len(first_seen) - len(partner_list),
            "templates_active": templates_active,
            "employees_active": employees_active,
            "avg_per_day": round(total / days, 1) if days else 0.0,
            "peak_date": peak["date"],
            "peak_count": peak["count"],
        },
        "trend": series,
        "partners": partner_list,
        "system": system_status(),
        "meta": {
            "calls_definition": CALLS_DEFINITION,
            "business_date": "work_date",
            "status_rule": (
                f"近 {ACTIVE_MAX_DAYS} 天有调用为活跃，"
                f"{ACTIVE_MAX_DAYS + 1}-{WARNING_MAX_DAYS} 天无调用为预警，"
                f"超过 {WARNING_MAX_DAYS} 天为停用"
            ),
            "tz": Config.TZ,
        },
    }
