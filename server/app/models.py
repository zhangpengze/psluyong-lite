"""
数据模型

两张表：
  admin_users  后台账号
  records      录用记录（= 工作日志）

模板与合作伙伴名单由员工在本机客户端维护，不经服务器，所以这里没有
templates / dict_items 两张表。
"""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import db, iso_utc, utcnow

# 记录状态
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


class AdminUser(db.Model):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    # admin 可维护字典/模板；viewer 只读
    role = Column(String(16), nullable=False, default="viewer")
    is_active = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "is_active": self.is_active,
            "last_login_at": iso_utc(self.last_login_at),
        }


class Record(db.Model):
    """一次生成 = 一条工作日志"""

    __tablename__ = "records"

    id = Column(Integer, primary_key=True)
    work_date = Column(Date, nullable=False, default=date.today, index=True)

    # 提交人（客户端配置的员工名），与"作者"不同：作者是稿件作者，员工是操作人
    employee_name = Column(String(100), nullable=False, index=True)

    # 模板由员工本机维护，template_id 存的是本机 PSD 文件名，因此放宽到 255
    template_id = Column(String(255), index=True)
    template_name = Column(String(200), nullable=False)
    partner_name = Column(String(100), nullable=False, default="", index=True)

    author = Column(String(200), nullable=False, default="", index=True)  # 替换1
    title = Column(String(200), nullable=False, default="", index=True)  # 替换2

    image_path = Column(String(500))
    thumb_path = Column(String(500))  # 列表页缩略图，上传时生成，避免列表拉原图
    image_filename = Column(String(255))

    client_machine = Column(String(100))
    ps_version = Column(String(64))
    status = Column(String(16), nullable=False, default=STATUS_SUCCESS, index=True)
    error_message = Column(String(500))
    warnings = Column(Text)  # 生成告警（字体缺失/文本溢出），JSON 数组字符串
    processing_ms = Column(Integer, default=0)

    is_deleted = Column(Boolean, nullable=False, default=False, index=True)
    deleted_at = Column(DateTime)  # 软删除时间，回收站排序与日后自动清理用
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)

    __table_args__ = (
        Index("idx_records_date_status", "work_date", "status"),
        Index("idx_records_employee_date", "employee_name", "work_date"),
        Index("idx_records_partner_date", "partner_name", "work_date"),
        Index("idx_records_created_at_id", "created_at", "id"),
        # 去重兜底：同一天 + 同一员工 + 同一模板 + 同作者 + 同标题
        # 只允许一条未删除记录。路由层的"先查后插"挡不住并发，必须由数据库兜底。
        #
        # 键的构成有讲究，两个都踩过：
        #   - 带上 employee_name：原来只按 (work_date, author, title) 判重，
        #     两个员工同一天做同一篇稿子时，第二个人被判"重复提交"，
        #     白跑一次 PS 还拿不到记录。
        #   - 带上 template_name：同一员工同一天用两个期刊模板出同一篇稿
        #     是真实业务，不能拦。拦了员工只能改标题造假，比重复记录更糟。
        # 剩下的语义是"同一个人用同一个模板重复做了同一件事"——那才是误操作。
        Index(
            "uq_records_dedup_active",
            "work_date",
            "employee_name",
            "template_name",
            "author",
            "title",
            unique=True,
            sqlite_where=text("is_deleted = 0"),
        ),
    )

    @property
    def has_image(self) -> bool:
        return bool(self.image_path)

    def to_dict(self, with_warnings: bool = False) -> dict:
        data = {
            "id": self.id,
            "work_date": self.work_date.isoformat() if self.work_date else None,
            "employee_name": self.employee_name,
            "template_id": self.template_id,
            "template_name": self.template_name,
            "partner_name": self.partner_name,
            "author": self.author,
            "title": self.title,
            "image_filename": self.image_filename,
            "has_image": self.has_image,
            "client_machine": self.client_machine,
            "ps_version": self.ps_version,
            "status": self.status,
            "error_message": self.error_message,
            "processing_ms": self.processing_ms,
            "created_at": iso_utc(self.created_at),
        }
        if with_warnings:
            try:
                data["warnings"] = json.loads(self.warnings) if self.warnings else []
            except (TypeError, ValueError):
                data["warnings"] = []
        return data


class AuditLog(db.Model):
    """后台写操作的审计流水。

    为什么值得单独一张表：删除/恢复/导出/重推这类动作以前只写 logger，
    容器日志被轮转掉就永久消失。误删一批记录、追查"谁动过"，那时候
    再想补是补不回来的。
    """

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    at = Column(DateTime, nullable=False, default=utcnow, index=True)
    actor = Column(String(64), nullable=False, default="-")  # 用户名，或 api_key
    action = Column(String(32), nullable=False, index=True)  # delete_record / restore_record / export_csv / push_report / login_failed
    target = Column(String(64))  # 如 record:123
    detail = Column(String(500))
    ip = Column(String(64))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "at": iso_utc(self.at),
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "ip": self.ip,
        }


def init_seed(app) -> None:
    """首次启动：建管理员。已存在则跳过。"""
    from .config import Config

    if not AdminUser.query.filter_by(username=Config.ADMIN_USERNAME).first():
        admin = AdminUser(username=Config.ADMIN_USERNAME, role="admin")
        admin.set_password(Config.ADMIN_PASSWORD)
        db.session.add(admin)

    db.session.commit()
