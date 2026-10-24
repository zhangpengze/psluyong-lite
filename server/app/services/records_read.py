"""记录读侧查询辅助：分页、详情、回收站、导出取数。

筛选语义本体（build_filtered_query）在 records_service.py——这里是围绕它的
取数组装，供 API 路由与后台页共用。纯查询、无写入、无 HTTP 概念。
"""

from __future__ import annotations

from datetime import date

from ..models import Record
from .records_service import build_filtered_query


def paginate(query, page: int, size: int):
    """分页取一页数据。越界页码夹回最后一页，别让大 offset 白扫一遍表。"""
    total = query.count()
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    rows = (
        query.order_by(Record.created_at.desc(), Record.id.desc())
        .limit(size)
        .offset((page - 1) * size)
        .all()
    )
    return rows, total, page, pages


def list_deleted_rows(start: date, end: date, limit: int = 50) -> list[Record]:
    """回收站：与主表同一日期区间内的软删除记录（恢复入口）。"""
    return (
        Record.query.filter(
            Record.is_deleted.is_(True),
            Record.work_date >= start,
            Record.work_date <= end,
        )
        .order_by(Record.deleted_at.desc().nullslast(), Record.id.desc())
        .limit(limit)
        .all()
    )


def count_deleted() -> int:
    return Record.query.filter(Record.is_deleted.is_(True)).count()


def get_record(record_id: int, *, is_deleted: bool | None = None) -> Record | None:
    """按 id 取记录。is_deleted=None 不过滤——回收站里也要能看详情，再决定是否恢复。"""
    query = Record.query.filter_by(id=record_id)
    if is_deleted is not None:
        query = query.filter(Record.is_deleted.is_(is_deleted))
    return query.first()


def list_for_export(start: date, end: date, *, limit: int, **filters) -> tuple[list[Record], int, dict]:
    """导出取数：先数总数再截断。

    静默砍掉第 N+1 行是最坏的一种"看起来对"——主管拿着少了一半的表去
    对账，没有任何信号提醒他。截断由路由层读 X-Export-* 响应头明说。
    limit 由路由层传入（EXPORT_MAX_ROWS 在路由模块里，测试会 monkeypatch 它）。
    """
    query, params = build_filtered_query(start, end, **filters)
    total = query.count()
    rows = (
        query.order_by(Record.work_date.desc(), Record.created_at.desc(), Record.id.desc())
        .limit(limit)
        .all()
    )
    return rows, total, params
