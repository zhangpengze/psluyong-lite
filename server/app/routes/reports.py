"""日报接口：预览与手动触发"""

from __future__ import annotations

import logging
from datetime import date, datetime

from flask import Blueprint, current_app, jsonify, request

from ..auth import admin_required, api_or_session

bp = Blueprint("reports_api", __name__, url_prefix="/api/v1/reports")
logger = logging.getLogger(__name__)


def _parse_day(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return date.today()


@bp.get("/preview")
@api_or_session
def preview():
    """只看内容，不落盘不推送"""
    from bot.report import build_daily_report

    day = _parse_day(request.args.get("date"))
    content = build_daily_report(current_app, day)
    return jsonify({"ok": True, "date": day.isoformat(), "content": content})


@bp.post("/run")
@admin_required
def run():
    """手动生成并推送（补跑某天日报时用）"""
    from bot.report import run_daily

    payload = request.get_json(silent=True) or {}
    day = _parse_day(payload.get("date"))
    push = bool(payload.get("push", True))

    result = run_daily(current_app, day, push=push)
    logger.info("手动触发日报 date=%s push=%s result=%s", day, push, result["message"])
    return jsonify({"ok": True, **result})
