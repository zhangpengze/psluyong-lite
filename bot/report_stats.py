"""日报区间统计：从数据库聚合计数（拆自 report.py）。

单日日报与区间日报共用这里的结果，由 report._render 渲染成正文。
只依赖 SQLAlchemy 查询，不碰推送与落盘——改动口径时只需要动这里。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import func


def _collect_stats(app, start: date, end: date) -> dict:
    """区间统计。单日日报是 start == end 的特例。"""
    from server.app.db import db
    from server.app.models import STATUS_FAILED, Record

    with app.app_context():
        base = db.session.query(func.count(Record.id)).filter(
            Record.is_deleted.is_(False),
            Record.work_date >= start,
            Record.work_date <= end,
        )
        total = base.scalar() or 0
        failed = base.filter(Record.status == STATUS_FAILED).scalar() or 0

        def group(column, limit: int = 10):
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
                .limit(limit)
                .all()
            )
            return [(name, count) for name, count in rows]

        by_template = group(Record.template_name)
        by_partner = group(Record.partner_name)

        # 合作伙伴 × 模板 二维分布：按合作伙伴段落里每个伙伴下面展开，
        # 让「农技推广中心 7 条」直接看到是农业加急 3 + 今日农业 4
        pt_rows = (
            db.session.query(Record.partner_name, Record.template_name, func.count(Record.id))
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

        by_day = (
            db.session.query(Record.work_date, func.count(Record.id))
            .filter(Record.is_deleted.is_(False), Record.work_date >= start, Record.work_date <= end)
            .group_by(Record.work_date)
            .order_by(Record.work_date.desc())
            .all()
        )

        failed_rows = (
            Record.query.filter(
                Record.is_deleted.is_(False),
                Record.work_date >= start,
                Record.work_date <= end,
                Record.status == STATUS_FAILED,
            )
            .order_by(Record.created_at.desc())
            .limit(5)
            .all()
        )

    # partner -> [(template, count)]，按数量降序；模板名空的归「未填模板」保证总数守恒
    partner_templates: dict[str, list[tuple[str, int]]] = {}
    for partner, template, count in pt_rows:
        partner_templates.setdefault(partner, []).append((template or "未填模板", count))
    for rows in partner_templates.values():
        rows.sort(key=lambda item: (-item[1], item[0]))

    return {
        "start": start,
        "end": end,
        "total": total,
        "failed": failed,
        "by_template": by_template,
        "by_partner": by_partner,
        "partner_templates": partner_templates,
        "by_day": [(d, c) for d, c in by_day],
        "failed_rows": failed_rows,
    }


def stats_fingerprint(stats: dict) -> str:
    """数据指纹：内容变没变，只看**数据**，不看渲染出来的文字。

    为什么不用正文哈希：正文里有「生成时间：」和「（终版）」标记，
    同一天的同一批数据每次渲染都长得不一样，拿正文比就永远"变了"，
    次日终版会把一份没变化的日报再推一遍。

    也正因为指纹只认数据，18:00 之后补录的记录（total/明细变了）会让指纹变，
    这正是"该补推终版"的判据。
    """
    payload = {
        "start": stats["start"].isoformat(),
        "end": stats["end"].isoformat(),
        "total": stats["total"],
        "failed": stats["failed"],
        "by_day": [[d.isoformat(), c] for d, c in stats["by_day"]],
        "by_template": [list(x) for x in stats["by_template"]],
        "by_partner": [list(x) for x in stats["by_partner"]],
        "partner_templates": {k: [list(v) for v in vs] for k, vs in stats["partner_templates"].items()},
        "failed_rows": [
            [r.employee_name, r.author, r.title, r.error_message or ""] for r in stats["failed_rows"]
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
