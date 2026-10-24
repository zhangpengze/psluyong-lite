"""审计流水读侧查询：分页列表与筛选用动作维度。

纯查询、无写入（写入在 audit.log_action）、无 HTTP 概念。
动作中文名映射（ACTION_LABELS）留在 audit 模块由视图层组装——
那是展示文案，不是取数。
"""

from __future__ import annotations

from ..db import db
from ..models import AuditLog


def list_entries(action: str, page: int, size: int = 50):
    """按动作筛选的分页流水。越界页码夹回最后一页，别让大 offset 白扫表。"""
    query = AuditLog.query
    if action:
        query = query.filter(AuditLog.action == action)

    total = query.count()
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    rows = (
        query.order_by(AuditLog.id.desc())
        .limit(size)
        .offset((page - 1) * size)
        .all()
    )
    return rows, total, page, pages


def action_values() -> list[str]:
    """库中出现过的动作值（升序），供筛选下拉与标签映射。"""
    return [
        value
        for (value,) in db.session.query(AuditLog.action)
        .distinct()
        .order_by(AuditLog.action)
        .all()
    ]
