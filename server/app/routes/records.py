"""录用记录（= 工作日志）接口

路由层职责只有三步：取参数并校验 → 调 service → 组装响应。
查询构建、判重、事务编排（含失败回填）、筛选 facet 全部下沉在
server/app/services/（records_service / records_read / records_lifecycle），
本文件不直接碰数据库。
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from flask import Blueprint, Response, jsonify, request, send_file

from ..audit import log_action
from ..auth import admin_required, api_key_required, api_or_session, csrf_protect, current_user
from ..models import STATUS_FAILED, STATUS_SUCCESS
from ..services import records_lifecycle, records_read, records_service
from ..storage import ImageValidationError, resolve_image_path
from . import records_csv

bp = Blueprint("records", __name__, url_prefix="/api/v1/records")
logger = logging.getLogger(__name__)

MAX_AUTHOR = 200
MAX_TITLE = 200
MAX_PARTNER = 100

# 导出上限：内网小团队够用；真触顶时行数会说话，不至于把内存吃穿
EXPORT_MAX_ROWS = 20000


def _clean(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def _read_filters() -> dict:
    """从 query string 取筛选参数（只取值，语义交给 service 统一裁剪/匹配）"""
    return {
        "employee": request.args.get("employee", ""),
        "partner": request.args.get("partner", ""),
        "template_id": request.args.get("template_id", ""),
        "status": request.args.get("status", ""),
        "keyword": request.args.get("q", ""),
    }


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

    work_date = records_service.parse_date(form.get("work_date"), date.today())
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

    try:
        record = records_service.create_record(
            work_date=work_date,
            employee_name=employee_name,
            template_id=template_id,
            template_name=template_name,
            partner_name=_clean(form.get("partner_name"), MAX_PARTNER),
            author=author,
            title=title,
            status=status,
            error_message=_clean(form.get("error_message"), 500) or None,
            client_machine=_clean(form.get("client_machine"), 100) or None,
            ps_version=_clean(form.get("ps_version"), 64) or None,
            processing_ms=processing_ms,
            warnings=parsed_warnings,
            upload=request.files.get("image"),
        )
    except records_service.DuplicateRecord as e:
        return (
            jsonify({"ok": False, "error": str(e), "duplicate": True, "data": e.record.to_dict()}),
            409,
        )
    except records_service.ConcurrentDuplicate as e:
        return jsonify({"ok": False, "error": str(e), "duplicate": True}), 409
    except ImageValidationError as e:
        # 400 而不是 500：这是客户端送错了东西，重试也不会成功。
        # 带上 retryable=False 是为了让客户端不要把它当成"可重试的网络错误"
        # 塞进离线队列——那会变成永远补不成功的一条毒记录。
        return jsonify({"ok": False, "error": str(e), "retryable": False}), 400

    return jsonify({"ok": True, "data": record.to_dict(with_warnings=True)}), 201


@bp.get("")
@api_or_session
def list_records():
    """记录列表：日期区间 + 员工/合作伙伴/模板/状态筛选 + 关键词 + 分页"""
    start, end = records_service.resolve_range(request.args.get("start"), request.args.get("end"))
    query, _params = records_service.build_filtered_query(start, end, **_read_filters())

    try:
        page = max(1, int(request.args.get("page", 1)))
        size = min(200, max(1, int(request.args.get("size", 20))))
    except ValueError:
        page, size = 1, 20

    rows, total, page, pages = records_read.paginate(query, page, size)
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


@bp.get("/export")
@api_or_session
def export_records():
    """按当前筛选条件导出 CSV（UTF-8 BOM，Excel 直接双击打开不乱码）"""
    start, end = records_service.resolve_range(request.args.get("start"), request.args.get("end"))
    # 截断上限是路由模块的全局量（测试会 monkeypatch EXPORT_MAX_ROWS），必须在此读取
    rows, total, params = records_read.list_for_export(
        start, end, limit=EXPORT_MAX_ROWS, **_read_filters()
    )
    truncated = total > len(rows)
    body = records_csv.write_records_csv(rows)

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
        body,
        # charset 只写一处：mimetype 再带 charset 会拼成重复的
        # "text/csv; charset=utf-8; charset=utf-8"
        mimetype="text/csv",
        headers={**headers, "Content-Type": "text/csv; charset=utf-8"},
    )


@bp.get("/<int:record_id>")
@api_or_session
def get_record(record_id: int):
    record = records_read.get_record(record_id)
    if not record:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True, "data": record.to_dict(with_warnings=True)})


@bp.get("/<int:record_id>/image")
@api_or_session
def get_record_image(record_id: int):
    # 软删除记录的文件刻意保留，回收站要能预览缩略图与原图
    record = records_read.get_record(record_id)
    if not record:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    want_thumb = request.args.get("thumb") == "1"
    stored = record.thumb_path if want_thumb and record.thumb_path else record.image_path
    if not stored:
        return jsonify({"ok": False, "error": "该记录没有图片"}), 404

    # 库里可能存着别的机器上的绝对路径（换机迁移），统一解析到当前 IMAGES_DIR
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
    record = records_read.get_record(record_id, is_deleted=False)
    if not record:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    records_lifecycle.soft_delete(record)
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
    record = records_read.get_record(record_id, is_deleted=True)
    if not record:
        return jsonify({"ok": False, "error": "记录不存在或未被删除"}), 404

    try:
        records_lifecycle.restore(record)
    except records_service.RestoreConflict as e:
        return jsonify({"ok": False, "error": str(e)}), 409

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
    return jsonify({"ok": True, **records_service.facet_values()})
