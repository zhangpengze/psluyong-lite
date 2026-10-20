"""录用记录（= 工作日志）接口"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request, send_file
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from ..auth import admin_required, api_key_required, api_or_session, current_user
from ..db import db
from ..models import STATUS_FAILED, STATUS_SUCCESS, Record
from ..storage import delete_image, save_image

bp = Blueprint("records", __name__, url_prefix="/api/v1/records")
logger = logging.getLogger(__name__)

MAX_AUTHOR = 200
MAX_TITLE = 200
MAX_PARTNER = 100

# LIKE 通配符转义符。搜 "50%" 必须匹配字面 %，否则会命中全表。
_LIKE_ESCAPE = "\\"


def _like_contains(keyword: str) -> str:
    """把关键词变成安全的 LIKE 模式（前后模糊）"""
    escaped = (
        keyword.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return default


def _clean(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


@bp.post("")
@api_key_required
def create_record():
    """客户端提交一条录用记录（multipart，图片可选）"""
    form = request.form

    employee_name = _clean(form.get("employee_name"), 100)
    author = _clean(form.get("author"), MAX_AUTHOR)
    title = _clean(form.get("title"), MAX_TITLE)
    if not employee_name:
        return jsonify({"ok": False, "error": "缺少 employee_name"}), 400
    if not author or not title:
        return jsonify({"ok": False, "error": "作者和标题不能为空"}), 400

    work_date = _parse_date(form.get("work_date"), date.today())
    status = form.get("status", STATUS_SUCCESS)
    if status not in (STATUS_SUCCESS, STATUS_FAILED):
        status = STATUS_SUCCESS

    # 模板名由客户端直接给（模板归员工本机管，服务端没有模板表可查）
    template_id = _clean(form.get("template_id"), 255) or None
    template_name = _clean(form.get("template_name"), 200)
    if not template_name:
        return jsonify({"ok": False, "error": "缺少模板名称"}), 400

    # 去重：同一天 + 同作者 + 同标题
    existing = Record.query.filter_by(
        work_date=work_date, author=author, title=title, is_deleted=False
    ).first()
    if existing:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "今天已录入过同一作者+标题，请勿重复提交",
                    "duplicate": True,
                    "data": existing.to_dict(),
                }
            ),
            409,
        )

    image_path = thumb_path = image_filename = ""
    upload = request.files.get("image")
    if upload and upload.filename:
        image_path, thumb_path, image_filename = save_image(upload, work_date)

    warnings_raw = form.get("warnings", "")
    try:
        warnings_json = json.dumps(json.loads(warnings_raw), ensure_ascii=False) if warnings_raw else None
    except (TypeError, ValueError):
        warnings_json = None

    try:
        processing_ms = int(form.get("processing_ms") or 0)
    except ValueError:
        processing_ms = 0

    record = Record(
        work_date=work_date,
        employee_name=employee_name,
        template_id=template_id,
        template_name=template_name,
        partner_name=_clean(form.get("partner_name"), MAX_PARTNER),
        author=author,
        title=title,
        image_path=image_path or None,
        thumb_path=thumb_path or None,
        image_filename=image_filename or None,
        client_machine=_clean(form.get("client_machine"), 100) or None,
        ps_version=_clean(form.get("ps_version"), 64) or None,
        status=status,
        error_message=_clean(form.get("error_message"), 500) or None,
        warnings=warnings_json,
        processing_ms=processing_ms,
    )
    db.session.add(record)
    try:
        db.session.commit()
    except IntegrityError:
        # 并发下两个请求同时通过了上面的"先查"，靠唯一索引兜底
        db.session.rollback()
        delete_image(image_path, thumb_path)
        return jsonify({"ok": False, "error": "记录已存在（并发提交）", "duplicate": True}), 409

    return jsonify({"ok": True, "data": record.to_dict(with_warnings=True)}), 201


@bp.get("")
@api_or_session
def list_records():
    """记录列表：日期区间 + 员工/合作伙伴/模板/状态筛选 + 关键词 + 分页"""
    today = date.today()
    start = _parse_date(request.args.get("start"), today - timedelta(days=6))
    end = _parse_date(request.args.get("end"), today)
    if start > end:
        start, end = end, start

    query = Record.query.filter(Record.is_deleted.is_(False))
    query = query.filter(Record.work_date >= start, Record.work_date <= end)

    if employee := request.args.get("employee"):
        query = query.filter(Record.employee_name == employee)
    if partner := request.args.get("partner"):
        query = query.filter(Record.partner_name == partner)
    if template_id := request.args.get("template_id"):
        query = query.filter(Record.template_id == template_id)
    if status := request.args.get("status"):
        query = query.filter(Record.status == status)
    if keyword := (request.args.get("q") or "").strip():
        like = _like_contains(keyword)
        query = query.filter(
            db.or_(
                Record.author.like(like, escape=_LIKE_ESCAPE),
                Record.title.like(like, escape=_LIKE_ESCAPE),
                Record.partner_name.like(like, escape=_LIKE_ESCAPE),
                Record.template_name.like(like, escape=_LIKE_ESCAPE),
                Record.employee_name.like(like, escape=_LIKE_ESCAPE),
            )
        )

    try:
        page = max(1, int(request.args.get("page", 1)))
        size = min(200, max(1, int(request.args.get("size", 20))))
    except ValueError:
        page, size = 1, 20

    total = query.count()
    rows = (
        query.order_by(Record.created_at.desc(), Record.id.desc())
        .limit(size)
        .offset((page - 1) * size)
        .all()
    )

    return jsonify(
        {
            "ok": True,
            "items": [r.to_dict() for r in rows],
            "total": total,
            "page": page,
            "size": size,
            "pages": (total + size - 1) // size,
        }
    )


@bp.get("/<int:record_id>")
@api_or_session
def get_record(record_id: int):
    record = Record.query.filter_by(id=record_id, is_deleted=False).first()
    if not record:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True, "data": record.to_dict(with_warnings=True)})


@bp.get("/<int:record_id>/image")
@api_or_session
def get_record_image(record_id: int):
    record = Record.query.filter_by(id=record_id, is_deleted=False).first()
    if not record:
        return jsonify({"ok": False, "error": "记录不存在"}), 404

    want_thumb = request.args.get("thumb") == "1"
    path = record.thumb_path if want_thumb and record.thumb_path else record.image_path
    if not path:
        return jsonify({"ok": False, "error": "该记录没有图片"}), 404

    from pathlib import Path

    file_path = Path(path)
    if not file_path.exists():
        logger.warning("图片文件缺失: record=%s path=%s", record_id, path)
        return jsonify({"ok": False, "error": "图片文件已丢失"}), 404

    return send_file(file_path, conditional=True)


@bp.delete("/<int:record_id>")
@admin_required
def delete_record(record_id: int):
    """软删除。图片保留，避免误删后无法恢复。"""
    record = Record.query.filter_by(id=record_id, is_deleted=False).first()
    if not record:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    record.is_deleted = True
    db.session.commit()
    logger.info("记录软删除 id=%s by=%s", record_id, current_user().username if current_user() else "-")
    return jsonify({"ok": True})


@bp.get("/meta/options")
@api_or_session
def filter_options():
    """筛选下拉的可选值（只取未删除记录里出现过的）"""
    def distinct(column):
        rows = (
            db.session.query(column)
            .filter(Record.is_deleted.is_(False), column.isnot(None), column != "")
            .distinct()
            .order_by(column)
            .all()
        )
        return [r[0] for r in rows]

    return jsonify(
        {
            "ok": True,
            "employees": distinct(Record.employee_name),
            "partners": distinct(Record.partner_name),
            "templates": distinct(Record.template_name),
        }
    )
