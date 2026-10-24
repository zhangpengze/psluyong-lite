"""统计接口"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, Response, jsonify, request
from sqlalchemy import func

from ..audit import log_action
from ..auth import api_or_session
from ..db import db
from ..models import STATUS_FAILED, STATUS_SUCCESS, Record
from ..overview import RangeError, get_overview, parse_limit, parse_range
from ..panels import overview_csv, overview_html

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


@bp.get("/overview")
@api_or_session
def overview():
    """仪表盘概览：KPI + 趋势 + 合作伙伴明细（含各模板用量），一次拿齐。

    range=today|yesterday|7d|30d|custom（custom 另带 start/end，YYYY-MM-DD）。
    响应同时带结构化字段和渲染好的 html 片段——首屏 Jinja 与轮询 JS
    复用同一份标记（panels.py 生成），避免两套渲染漂移。
    """
    try:
        start, end, label, preset = parse_range(request.args)
    except RangeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    payload = dict(get_overview(start, end, label, preset))
    payload["html"] = overview_html(payload, parse_limit(request.args))
    return jsonify(payload)


@bp.get("/overview/export")
@api_or_session
def overview_export():
    """概览导出 CSV：汇总 + 伙伴明细 + 各伙伴模板用量（UTF-8 BOM，Excel 直开）。

    不受 limit 截断影响——导出就是要全量。记审计：导出不写库，但
    "谁把数据拷走了"必须查得到（与记录页导出同一处理）。
    """
    try:
        start, end, label, preset = parse_range(request.args)
    except RangeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    ov = get_overview(start, end, label, preset)
    log_action("export_csv", detail=f"概览 {start}~{end} · {len(ov['partners'])} 家伙伴")
    filename = f"overview_{start:%Y%m%d}_{end:%Y%m%d}.csv"
    return Response(
        overview_csv(ov),
        # charset 只写一处：mimetype 再带 charset 会让 Flask 重复拼接成
        # "text/csv; charset=utf-8; charset=utf-8"
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )
