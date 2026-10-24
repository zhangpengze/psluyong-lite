"""记录生命周期编排：软删除与从回收站恢复。

与 records_service.create_record 同层的写编排——落库 + 概览缓存失效在一个
事务语义里完成；审计日志（谁删的、谁恢复的）由路由层在成功后补写，因为
"操作者"属于 HTTP 语境，不该下沉进服务层。
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from ..db import db, utcnow
from ..models import STATUS_SUCCESS, Record
from ..overview import invalidate as invalidate_overview
from .records_service import RestoreConflict


def soft_delete(record: Record) -> None:
    """软删除。图片保留，避免误删后无法恢复。"""
    record.is_deleted = True
    record.deleted_at = utcnow()
    db.session.commit()
    invalidate_overview()


def restore(record: Record) -> None:
    """从回收站恢复一条软删除的记录。

    可能撞上部分唯一索引：同一天 + 同一员工 + 同一模板 + 同作者 + 同标题
    只允许一条未删除的**成功**记录（失败记录不参与判重，见 models 说明）。
    冲突抛 RestoreConflict，消息直接给用户看。
    """
    if record.status == STATUS_SUCCESS:
        conflict = Record.query.filter_by(
            work_date=record.work_date,
            employee_name=record.employee_name,
            template_name=record.template_name,
            author=record.author,
            title=record.title,
            status=STATUS_SUCCESS,
            is_deleted=False,
        ).first()
        if conflict:
            raise RestoreConflict(
                "已存在一条未删除的相同记录（同一天 + 同一员工 + 同一模板 + 同作者 + 同标题），无法恢复"
            )

    record.is_deleted = False
    record.deleted_at = None
    try:
        db.session.commit()
    except IntegrityError:
        # 并发下先查后写漏了，靠唯一索引兜底
        db.session.rollback()
        raise RestoreConflict("恢复失败：与现有记录冲突") from None
    invalidate_overview()
