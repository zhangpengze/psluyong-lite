"""统计接口"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from ..auth import api_or_session
from ..db import db
from ..models import STATUS_FAILED, STATUS_SUCCESS, Record

bp = Blueprint("stats", __name__, url_prefix="/api/v1/stats")


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return default


def _group_count(start: date, end: date, column) -> list[dict]:
    rows = (
        db.session.query(column, func.count(Record.id))
        .filter(
            Record.is_deleted.is_(False),
            Record.work_date >= start,
            Record.work_date <= end,
            column.isnot(None),
            column != "",
        )
        .group_by(column)
        .order_by(func.count(Record.id).desc())
        .all()
    )
    return [{"name": name, "count": count} for name, count in rows]


def _totals(start: date, end: date) -> dict:
    base = db.session.query(func.count(Record.id)).filter(
        Record.is_deleted.is_(False),
        Record.work_date >= start,
        Record.work_date <= end,
    )
    total = base.scalar() or 0
    failed = base.filter(Record.status == STATUS_FAILED).scalar() or 0
    return {
        "total": total,
        "success": total - failed,
        "failed": failed,
        "success_rate": round((total - failed) / total * 100, 1) if total else 0.0,
    }


@bp.get("/daily")
@api_or_session
def daily():
    """单日汇总：总量 + 按员工/模板/合作伙伴分布"""
    day = _parse_date(request.args.get("date"), date.today())
    return jsonify(
        {
            "ok": True,
            "date": day.isoformat(),
            **_totals(day, day),
            "by_employee": _group_count(day, day, Record.employee_name),
            "by_template": _group_count(day, day, Record.template_name),
            "by_partner": _group_count(day, day, Record.partner_name),
        }
    )


@bp.get("/trend")
@api_or_session
def trend():
    """近 N 天每日生成量，用于仪表盘折线图"""
    try:
        days = min(90, max(1, int(request.args.get("days", 7))))
    except ValueError:
        days = 7

    end = _parse_date(request.args.get("end"), date.today())
    start = end - timedelta(days=days - 1)

    rows = (
        db.session.query(Record.work_date, func.count(Record.id))
        .filter(
            Record.is_deleted.is_(False),
            Record.work_date >= start,
            Record.work_date <= end,
        )
        .group_by(Record.work_date)
        .all()
    )
    counted = {d: c for d, c in rows}

    series = []
    cursor = start
    while cursor <= end:
        series.append({"date": cursor.isoformat(), "count": counted.get(cursor, 0)})
        cursor += timedelta(days=1)

    return jsonify(
        {
            "ok": True,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "series": series,
            **_totals(start, end),
            "by_employee": _group_count(start, end, Record.employee_name),
            "by_template": _group_count(start, end, Record.template_name),
            "by_partner": _group_count(start, end, Record.partner_name),
        }
    )
