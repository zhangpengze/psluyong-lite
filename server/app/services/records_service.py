"""录用记录（工作日志）业务服务：筛选查询、判重、写入编排。

分层约定：本模块不 import flask.request / Blueprint / g / session，
不拼 HTTP 响应——路由层取参数传进来，拿到结果（或业务异常）后自行组装响应。
列表、导出、后台页三处的筛选语义只有这一份实现，避免两边漂移
（搜索 LIKE 转义同理，见 server/app/search.py）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from ..db import db, utcnow
from ..models import STATUS_FAILED, STATUS_SUCCESS, Record
from ..overview import invalidate as invalidate_overview
from ..search import LIKE_ESCAPE, like_contains
from ..storage import delete_image, save_image

logger = logging.getLogger(__name__)


def parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return default


def resolve_range(start_raw: str | None, end_raw: str | None, days_back: int = 6) -> tuple[date, date]:
    """把起止字符串解析成 (start, end)，缺省近 N 天；start>end 时自动对调。"""
    today = date.today()
    start = parse_date(start_raw, today - timedelta(days=days_back))
    end = parse_date(end_raw, today)
    if start > end:
        start, end = end, start
    return start, end


def build_filtered_query(
    start: date,
    end: date,
    *,
    employee: str = "",
    partner: str = "",
    template_id: str = "",
    status: str = "",
    keyword: str = "",
):
    """按参数构造记录查询（列表 / 导出 / 后台页共用同一套筛选语义）。

    返回 (query, params)：params 只回显当前生效的筛选值（start/end 必有），
    供导出拼文件名与审计明细用。
    """
    query = Record.query.filter(Record.is_deleted.is_(False))
    query = query.filter(Record.work_date >= start, Record.work_date <= end)
    params: dict = {"start": start, "end": end}

    if employee := (employee or "").strip():
        query = query.filter(Record.employee_name == employee)
        params["employee"] = employee
    if partner := (partner or "").strip():
        query = query.filter(Record.partner_name == partner)
        params["partner"] = partner
    if template_id := (template_id or "").strip():
        query = query.filter(Record.template_id == template_id)
        params["template_id"] = template_id
    if status := (status or "").strip():
        query = query.filter(Record.status == status)
        params["status"] = status
    if keyword := (keyword or "").strip():
        # 必须走 like_contains + escape：直接 f"%{keyword}%" 会让 SQLite 把
        # 关键词里的 % 和 _ 当通配符，主管搜「50%」等于把全表捞出来当搜索结果
        like = like_contains(keyword)
        query = query.filter(
            db.or_(
                Record.author.like(like, escape=LIKE_ESCAPE),
                Record.title.like(like, escape=LIKE_ESCAPE),
                Record.partner_name.like(like, escape=LIKE_ESCAPE),
                Record.template_name.like(like, escape=LIKE_ESCAPE),
                # 员工名也要能搜：API 侧一直支持，后台页面原来漏了，
                # 同一个词在两边给出不同结果会让人怀疑数据
                Record.employee_name.like(like, escape=LIKE_ESCAPE),
            )
        )
        params["q"] = keyword

    return query, params


_FACET_COLUMNS = {
    "employees": Record.employee_name,
    "partners": Record.partner_name,
    "templates": Record.template_name,
}


def facet_values(*names: str) -> dict[str, list[str]]:
    """筛选下拉的可选值（只取未删除记录里出现过的）。不传 names = 三组全取。"""
    result = {}
    for name in names or tuple(_FACET_COLUMNS):
        column = _FACET_COLUMNS[name]
        rows = (
            db.session.query(column)
            .filter(Record.is_deleted.is_(False), column.isnot(None), column != "")
            .distinct()
            .order_by(column)
            .all()
        )
        result[name] = [r[0] for r in rows]
    return result


# ---------- 写入编排 ----------

class DuplicateRecord(Exception):
    """同一成功记录已存在（先查挡并发前的重复提交）"""

    def __init__(self, record: Record):
        super().__init__("你今天已经用这个模板录入过同一作者+标题，请勿重复提交")
        self.record = record


class ConcurrentDuplicate(Exception):
    """并发下先查失效，由部分唯一索引兜底拦下"""

    def __init__(self):
        super().__init__("记录已存在（并发提交）")


class RestoreConflict(Exception):
    """恢复会撞上部分唯一索引，消息直接给用户看"""


def find_duplicate(work_date: date, employee_name: str, template_name: str, author: str, title: str) -> Record | None:
    """去重：同一天 + 同一员工 + 同一模板 + 同作者 + 同标题的成功记录。

    两个人做同一篇稿子、或同一个人用两个模板出同一篇稿，都不该互相挡；
    "同一个人用同一个模板重复做成了同一件事"才是真正的误操作信号。
    只跟**成功**记录比：失败记录也要留档，但拿它挡重试会让员工修好 PS 后
    再也录不进这一篇（只能改标题造假），这是必修的洞。
    """
    return Record.query.filter_by(
        work_date=work_date,
        employee_name=employee_name,
        template_name=template_name,
        author=author,
        title=title,
        status=STATUS_SUCCESS,
        is_deleted=False,
    ).first()


def create_record(
    *,
    work_date: date,
    employee_name: str,
    template_id: str | None,
    template_name: str,
    partner_name: str,
    author: str,
    title: str,
    status: str,
    error_message: str | None,
    client_machine: str | None,
    ps_version: str | None,
    processing_ms: int,
    warnings: list,
    upload,  # werkzeug FileStorage | None：路由从请求文件里取出后传入，服务层只读它
) -> Record:
    """判重 → 存图 → 失败记录回填 → 落库。抛 DuplicateRecord / ConcurrentDuplicate /
    ImageValidationError（storage 层），由路由层映射为 409 / 400。"""
    existing = find_duplicate(work_date, employee_name, template_name, author, title)
    if existing is not None:
        raise DuplicateRecord(existing)

    image_path = thumb_path = image_filename = ""
    if upload and upload.filename:
        image_path, thumb_path, image_filename = save_image(upload, work_date)

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
            warnings.append(f"上次失败：{prior_error[:200]}，本次已成功")
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
    record.partner_name = partner_name
    record.author = author
    record.title = title
    record.image_path = image_path or None
    record.thumb_path = thumb_path or None
    record.image_filename = image_filename or None
    record.client_machine = client_machine
    record.ps_version = ps_version
    record.status = status
    record.error_message = error_message
    record.warnings = json.dumps(warnings, ensure_ascii=False) if warnings else None
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
        raise ConcurrentDuplicate from None

    if status == STATUS_FAILED:
        _notify_failure(record)

    # 概览有 15 秒缓存：不清缓存，主管盯着页面要等一个轮询周期才看到新记录
    invalidate_overview()
    return record


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
        from bot.report import push_alert  # 函数内延迟 import：不引入模块级 import bot

        try:
            ok, message = push_alert(content)
            if not ok:
                logger.info("失败告警未发送: %s", message)
        except Exception:  # noqa: BLE001 - 通知失败绝不能影响已落库的记录
            logger.exception("失败告警推送异常")

    threading.Thread(target=_send, daemon=True).start()
