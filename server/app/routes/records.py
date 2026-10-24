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
from ..overview import invalidate as invalidate_overview
from ..search import LIKE_ESCAPE, like_contains
from ..storage import ImageValidationError, delete_image, save_image

bp = Blueprint("records", __name__, url_prefix="/api/v1/records")
logger = logging.getLogger(__name__)

MAX_AUTHOR = 200
MAX_TITLE = 200
MAX_PARTNER = 100

# 导出上限：内网小团队够用；真触顶时行数会说话，不至于把内存吃穿
EXPORT_MAX_ROWS = 20000


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
    # 曾有「图片路径」列：Record.to_dict() 不含该键，列头有、数据恒空（审计 B3）。
    # 按 D1 方案 A 删列——导出文件也不该外泄服务器文件系统路径。
]


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return default


def _clean(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def _build_filtered_query(force_employee: str | None = None):
    """按请求参数构造记录查询（列表与导出共用同一套筛选语义）。

    force_employee：给客户端"只看自己"用（当前由客户端自己传 employee 参数）。
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

    employee = force_employee or (request.args.get("employee") or "").strip()
    if employee:
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
        like = like_contains(keyword)
        query = query.filter(
            db.or_(
                Record.author.like(like, escape=LIKE_ESCAPE),
                Record.title.like(like, escape=LIKE_ESCAPE),
                Record.partner_name.like(like, escape=LIKE_ESCAPE),
                Record.template_name.like(like, escape=LIKE_ESCAPE),
                Record.employee_name.like(like, escape=LIKE_ESCAPE),
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
    # "同一个人用同一个模板重复做成了同一件事"才是真正的误操作信号。
    # 只跟**成功**记录比：失败记录也要留档，但拿它挡重试会让员工修好 PS 后
    # 再也录不进这一篇（只能改标题造假），这是必修的洞。
    existing = Record.query.filter_by(
        work_date=work_date,
        employee_name=employee_name,
        template_name=template_name,
        author=author,
        title=title,
        status=STATUS_SUCCESS,
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
        try:
            image_path, thumb_path, image_filename = save_image(upload, work_date)
        except ImageValidationError as e:
            # 400 而不是 500：这是客户端送错了东西，重试也不会成功。
            # 带上 duplicate=False 是为了让客户端不要把它当成"可重试的网络错误"
            # 塞进离线队列——那会变成永远补不成功的一条毒记录。
            return jsonify({"ok": False, "error": str(e), "retryable": False}), 400

    warnings_raw = form.get("warnings", "")
    try:
        parsed_warnings = json.loads(warnings_raw) if warnings_raw else []
    except (TypeError, ValueError):
        parsed_warnings = []
    if not isinstance(parsed_warnings, list):
        parsed_warnings = []

    try:
        processing_ms = int(form.get("processing_ms") or 0)
    except ValueError:
        processing_ms = 0

    # 同一次工作重试：直接回填那条失败记录，而不是新增一条。
    #
    # 为什么必须回填：失败也入库是为了排障（"今天怎么这么慢"），但如果不回填，
    # 同一篇稿会同时存在多条 failed + 一条 success——"今日出图"多算、成功率被
    # 已经解决的历史失败永久拉低，主管看到的是假数字；连续失败三次的稿子在列表里
    # 还会占三行。回填把上一次的失败原因并进 warnings，排障信息不丢。
    record = (
        Record.query.filter_by(
            work_date=work_date,
            employee_name=employee_name,
            template_name=template_name,
            author=author,
            title=title,
            status=STATUS_FAILED,
            is_deleted=False,
        )
        .order_by(Record.created_at.desc())
        .first()
    )
    if record is not None:
        prior_error = (record.error_message or "").strip()
        if prior_error and status == STATUS_SUCCESS:
            parsed_warnings.append(f"上次失败：{prior_error[:200]}，本次已成功")
        old_image, old_thumb = record.image_path, record.thumb_path
        if old_image and old_image != image_path:
            delete_image(old_image, old_thumb)
        backfilled = True
    else:
        record = Record()
        backfilled = False

    record.work_date = work_date
    record.employee_name = employee_name
    record.template_id = template_id
    record.template_name = template_name
    record.partner_name = _clean(form.get("partner_name"), MAX_PARTNER)
    record.author = author
    record.title = title
    record.image_path = image_path or None
    record.thumb_path = thumb_path or None
    record.image_filename = image_filename or None
    record.client_machine = _clean(form.get("client_machine"), 100) or None
    record.ps_version = _clean(form.get("ps_version"), 64) or None
    record.status = status
    record.error_message = _clean(form.get("error_message"), 500) or None
    record.warnings = json.dumps(parsed_warnings, ensure_ascii=False) if parsed_warnings else None
    record.processing_ms = processing_ms
    record.is_deleted = False
    record.deleted_at = None
    if backfilled:
        # 回填的行沿用原 id，但把 created_at 刷成这次的完成时间：
        # 员工 14:30 才做成功，记录却显示 14:10（上次失败的时间），会被当成"又失败了"
        record.created_at = utcnow()

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

    # 概览有 15 秒缓存：不清缓存，主管盯着页面要等一个轮询周期才看到新记录
    invalidate_overview()
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

    # 先数总数再截断：静默砍掉第 20001 行是最坏的一种"看起来对"——
    # 主管拿着少了一半的表去对账，没有任何信号提醒他。截断要明说。
    total = query.count()
    rows = (
        query.order_by(Record.work_date.desc(), Record.created_at.desc(), Record.id.desc())
        .limit(EXPORT_MAX_ROWS)
        .all()
    )
    truncated = total > len(rows)

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
        "导出 CSV rows=%s total=%s truncated=%s by=%s filters=%s",
        len(rows),
        total,
        truncated,
        current_user().username if current_user() else "api_key",
        {k: v for k, v in params.items() if k not in ("start", "end")} or "无",
    )
    log_action(
        "export_csv",
        detail=f"{len(rows)} 行 · {params['start']}~{params['end']}"
        + (f" · 共 {total} 行被截断" if truncated else "")
        + (
            " · "
            + " ".join(f"{k}={v}" for k, v in params.items() if k not in ("start", "end"))
            if len(params) > 2
            else ""
        ),
    )
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if truncated:
        # 前端读到这个头就弹提示，别让人拿半张表去对账
        headers["X-Export-Truncated"] = "1"
        headers["X-Export-Total"] = str(total)
        headers["X-Export-Limit"] = str(EXPORT_MAX_ROWS)
    return Response(
        buf.getvalue(),
        # charset 只写一处：mimetype 再带 charset 会拼成重复的
        # "text/csv; charset=utf-8; charset=utf-8"
        mimetype="text/csv",
        headers={**headers, "Content-Type": "text/csv; charset=utf-8"},
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
    invalidate_overview()
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
    # 只允许一条未删除的**成功**记录（失败记录不参与判重，见 models 里的说明）
    conflict = None
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
    invalidate_overview()
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
