"""统计聚合查询：单日/区间汇总、分组计数、趋势序列。

供 routes/stats.py 使用的纯取数层。纯参数进、纯结果出——
不含 Flask 概念（request/Blueprint/g），错误即异常、由路由层映射。
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func

from ..db import db
from ..models import STATUS_FAILED, Record


def group_count(start: date, end: date, column) -> list[dict]:
    """区间内按 column 分组计数（仅统计非空值），按量降序。"""
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


def totals(start: date, end: date) -> dict:
    """区间汇总量：总量/成功/失败/成功率（失败=STATUS_FAILED 计数）。"""
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


def trend_series(start: date, end: date) -> list[dict]:
    """区间内每日生成量序列（缺失日期补 0，供折线图连续渲染）。"""
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
    return series
