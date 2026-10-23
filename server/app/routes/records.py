"""录用记录（= 工作日志）接口"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, jsonify, request, send_file
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from ..audit import log_action
from ..auth import admin_required, api_key_required, api_or_session, csrf_protect, current_user
from ..db import db, utcnow
from ..models import STATUS_FAILED, STATUS_SUCCESS, Record
from ..storage import delete_image, save_image

bp = Blueprint("records", __name__, url_prefix="/api/v1/records")
logger = logging.getLogger(__name__)

MAX_AUTHOR = 200
MAX_TITLE = 200
MAX_PARTNER = 100

# 导出上限：内网小团队够用；真触顶时行数会说话，不至于把内存吃穿
EXPORT_MAX_ROWS = 20000

# LIKE 通配符转义符。搜 "50%" 必须匹配字面 %，否则会命中全表。
_LIKE_ESCAPE = "\\"


def _notify_failure(record) -> None:
    """录用失败即时告警：起后台线程推群，不阻塞提交接口。

    注意：content 必须在线程外拼好——SQLAlchemy commit 后实例属性过期，
    后台线程里访问 record.xxx 会触发懒加载查库，而没有 app context 直接炸。
    失败记录本身已落库，告警只是通知——推送异常只记日志。
    """
    from server.app.config import Config

    if not Config.ALERT_ON_FAILURE:
        return

    content = (
        "⚠️ 录用失败\n"
        f"员工：{record.employee_name}｜模板：{record.template_name}\n"
        f"稿件：{record.author}/{record.title}\n"
        f"原因：{(record.error_message or '未知原因')[:80]}\n"
        f"时间：{datetime.now():%H:%M}"
    )

    def _send() -> None:
        from bot.report import push_alert

        try:
            ok, message = push_alert(content)
            if not ok:
                logger.info("失败告警未发送: %s", message)
        except Exception:  # noqa: BLE001 - 通知失败绝不能影响已落库的记录
            logger.exception("失败告警推送异常")

    threading.Thread(target=_send, daemon=True).start()

# CSV 导出列（顺序即表头）。中文表头给 Excel 用户看，列名保持与模型字段一致
_EXPORT_HEADERS = [
    ("work_date", "工作日期"),
    ("created_at", "提交时间"),
    ("employee_name", "员工"),
    ("template_name", "模板"),
    ("partner_name", "合作伙伴"),
    ("author", "作者"),
    ("title", "标题"),
    ("processing_ms", "耗时(秒)"),
    ("status", "状态"),
    ("error_message", "错误信息"),
    ("client_machine", "提交机器"),
    ("ps_version", "PS版本"),
    ("image_path", "图片路径"),
]


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


def _build_filtered_query():
    """按请求参数构造记录查询（列表与导出共用同一套筛选语义）。

    返回 (query, params)：params 回显当前生效的筛选值，供导出拼文件名用。
    """
    today = date.today()
    start = _parse_date(request.args.get("start"), today - timedelta(days=6))
    end = _parse_date(request.args.get("end"), today)
    if start > end:
        start, end = end, start

    query = Record.query.filter(Record.is_deleted.is_(False))
    query = query.filter(Record.work_date >= start, Record.work_date <= end)

    params = {"start": start, "end": end}

    if employee := (request.args.get("employee") or "").strip():
        query = query.filter(Record.employee_name == employee)
        params["employee"] = employee
    if partner := (request.args.get("partner") or "").strip():
        query = query.filter(Record.partner_name == partner)
        params["partner"] = partner
    if template_id := (request.args.get("template_id") or "").strip():
        query = query.filter(Record.template_id == template_id)
        params["template_id"] = template_id
    if status := (request.args.get("status") or "").strip():
        query = query.filter(Record.status == status)
        params["status"] = status
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
        params["q"] = keyword

    return query, params


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
    # 客户端时钟错乱 / 手工调用会把 2099 年的记录塞进统计，让"今日录用"
    # 之类的口径永久失真。留 +1 天给跨时区的凌晨提交，往回留一年。
    today = date.today()
    if work_date > today + timedelta(days=1) or work_date < today - timedelta(days=365):
        return jsonify({"ok": False, "error": "工作日期超出允许范围（近一年内）"}), 400
    status = form.get("status", STATUS_SUCCESS)
    if status not in (STATUS_SUCCESS, STATUS_FAILED):
        status = STATUS_SUCCESS

    # 模板名由客户端直接给（模板归员工本机管，服务端没有模板表可查）
    template_id = _clean(form.get("template_id"), 255) or None
    template_name = _clean(form.get("template_name"), 200)
    if not template_name:
        return jsonify({"ok": False, "error": "缺少模板名称"}), 400

    # 去重：同一天 + 同一员工 + 同一模板 + 同作者 + 同标题。
    # 两个人做同一篇稿子、或同一个人用两个模板出同一篇稿，都不该互相挡；
    # "同一个人用同一个模板重复做同一件事"才是真正的误操作信号。
    existing = Record.query.filter_by(
        work_date=work_date,
        employee_name=employee_name,
        template_name=template_name,
        author=author,
        title=title,
        is_deleted=False,
    ).first()
    if existing:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "你今天已经用这个模板录入过同一作者+标题，请勿重复提交",
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

    if status == STATUS_FAILED:
        _notify_failure(record)

    return jsonify({"ok": True, "data": record.to_dict(with_warnings=True)}), 201


@bp.get("")
@api_or_session
def list_records():
    """记录列表：日期区间 + 员工/合作伙伴/模板/状态筛选 + 关键词 + 分页"""
    query, _params = _build_filtered_query()

    try:
        page = max(1, int(request.args.get("page", 1)))
        size = min(200, max(1, int(request.args.get("size", 20))))
    except ValueError:
        page, size = 1, 20

    total = query.count()
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)  # 越界页码夹回最后一页，别让大 offset 白扫一遍表
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
            "pages": pages,
        }
    )


def _csv_safe(value) -> str:
    """防 CSV 公式注入：Excel 会把 =/+/-/@ 开头的单元格当公式执行。

    前置一个制表符（不可见），单元格按文本处理。数据源有客户端上报内容，
    这条不是洁癖——员工名/标题里真可能出现 = 开头的字符串。
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "\t" + text
    return text


@bp.get("/export")
@api_or_session
def export_records():
    """按当前筛选条件导出 CSV（UTF-8 BOM，Excel 直接双击打开不乱码）"""
    query, params = _build_filtered_query()

    rows = (
        query.order_by(Record.work_date.desc(), Record.created_at.desc(), Record.id.desc())
        .limit(EXPORT_MAX_ROWS)
        .all()
    )

    buf = io.StringIO()
    # utf-8-sig 即带 BOM 的 UTF-8：Excel 靠 BOM 识别中文编码，不加必乱码
    buf.write("\ufeff")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([header for _col, header in _EXPORT_HEADERS])

    for r in rows:
        data = r.to_dict()
        row = []
        for col, _header in _EXPORT_HEADERS:
            value = data.get(col)
            if col == "processing_ms" and value:
                value = round(value / 1000, 1)  # 列头已注明单位是秒
            elif col == "status":
                value = "成功" if value == STATUS_SUCCESS else "失败"
            row.append(_csv_safe(value))
        writer.writerow(row)

    filename = f"records_{params['start']:%Y%m%d}_{params['end']:%Y%m%d}.csv"
    logger.info(
        "导出 CSV rows=%s by=%s filters=%s",
        len(rows),
        current_user().username if current_user() else "api_key",
        {k: v for k, v in params.items() if k not in ("start", "end")} or "无",
    )
    log_action(
        "export_csv",
        detail=f"{len(rows)} 行 · {params['start']}~{params['end']}"
        + (
            " · "
            + " ".join(f"{k}={v}" for k, v in params.items() if k not in ("start", "end"))
            if len(params) > 2
            else ""
        ),
    )
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/<int:record_id>")
@api_or_session
def get_record(record_id: int):
    # 不过滤 is_deleted：回收站里也要能看详情，再决定是否恢复
    record = Record.query.filter_by(id=record_id).first()
    if not record:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True, "data": record.to_dict(with_warnings=True)})


@bp.get("/<int:record_id>/image")
@api_or_session
def get_record_image(record_id: int):
    # 软删除记录的文件刻意保留，回收站要能预览缩略图与原图
    record = Record.query.filter_by(id=record_id).first()
    if not record:
        return jsonify({"ok": False, "error": "记录不存在"}), 404

    want_thumb = request.args.get("thumb") == "1"
    stored = record.thumb_path if want_thumb and record.thumb_path else record.image_path
    if not stored:
        return jsonify({"ok": False, "error": "该记录没有图片"}), 404

    # 库里可能存着别的机器上的绝对路径（换机迁移），统一解析到当前 IMAGES_DIR
    from ..storage import resolve_image_path

    file_path = resolve_image_path(stored)
    if file_path is None or not file_path.exists():
        logger.warning("图片文件缺失: record=%s stored=%s resolved=%s", record_id, stored, file_path)
        return jsonify({"ok": False, "error": "图片文件已丢失"}), 404

    resp = send_file(file_path, conditional=True)
    # 缩略图内容按 record id 永恒不变，让浏览器直接吃缓存：翻页/刷新时
    # 少发 20 个条件请求，列表体感立刻不同。private 是防止中间代理
    # 把内网成品图缓存给没登录的人。
    resp.headers["Cache-Control"] = (
        "private, max-age=604800, immutable" if want_thumb else "private, max-age=60"
    )
    return resp


@bp.delete("/<int:record_id>")
@admin_required
@csrf_protect
def delete_record(record_id: int):
    """软删除。图片保留，避免误删后无法恢复。"""
    record = Record.query.filter_by(id=record_id, is_deleted=False).first()
    if not record:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    record.is_deleted = True
    record.deleted_at = utcnow()
    db.session.commit()
    logger.info("记录软删除 id=%s by=%s", record_id, current_user().username if current_user() else "-")
    log_action(
        "delete_record",
        f"record:{record_id}",
        f"{record.employee_name} {record.author}/{record.title}（{record.work_date}）",
    )
    return jsonify({"ok": True})


@bp.post("/<int:record_id>/restore")
@admin_required
@csrf_protect
def restore_record(record_id: int):
    """从回收站恢复一条软删除的记录"""
    record = Record.query.filter_by(id=record_id, is_deleted=True).first()
    if not record:
        return jsonify({"ok": False, "error": "记录不存在或未被删除"}), 404

    # 恢复可能撞上部分唯一索引：同一天 + 同一员工 + 同一模板 + 同作者 + 同标题
    # 只允许一条未删除记录
    conflict = Record.query.filter_by(
        work_date=record.work_date,
        employee_name=record.employee_name,
        template_name=record.template_name,
        author=record.author,
        title=record.title,
        is_deleted=False,
    ).first()
    if conflict:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "已存在一条未删除的相同记录（同一天 + 同一员工 + 同一模板 + 同作者 + 同标题），无法恢复",
                }
            ),
            409,
        )

    record.is_deleted = False
    record.deleted_at = None
    try:
        db.session.commit()
    except IntegrityError:
        # 并发下先查后写漏了，靠唯一索引兜底
        db.session.rollback()
        return jsonify({"ok": False, "error": "恢复失败：与现有记录冲突"}), 409

    logger.info("记录恢复 id=%s by=%s", record_id, current_user().username if current_user() else "-")
    log_action(
        "restore_record",
        f"record:{record_id}",
        f"{record.employee_name} {record.author}/{record.title}（{record.work_date}）",
    )
    return jsonify({"ok": True, "data": record.to_dict()})


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
