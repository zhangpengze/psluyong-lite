"""后台页面（服务端渲染）"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (
    Blueprint,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func

from ..audit import ACTION_LABELS, log_action
from ..auth import admin_required, current_user, generate_csrf_token, login_required
from ..db import db
from ..models import STATUS_FAILED, AdminUser, Record
from ..ratelimit import hit as rate_hit
from ..ratelimit import reset as rate_reset

bp = Blueprint("web", __name__)
logger = logging.getLogger(__name__)

# 登录失败限流：同一来源 5 分钟内最多 5 次失败尝试
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_S = 300


@bp.app_context_processor
def inject_current_user():
    """所有模板都能拿到当前账号，用于控制"删除"这类仅管理员可见的按钮"""
    return {"me": current_user()}


@bp.app_context_processor
def inject_csrf_token():
    """所有模板都能拿到 CSRF token，渲染进 <meta> 供 fetch 带上"""
    return {"csrf_token": generate_csrf_token}


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return default


def _totals(start: date, end: date) -> dict:
    base = db.session.query(func.count(Record.id)).filter(
        Record.is_deleted.is_(False),
        Record.work_date >= start,
        Record.work_date <= end,
    )
    total = base.scalar() or 0
    failed = base.filter(Record.status == STATUS_FAILED).scalar() or 0
    return {
        "total": total,
        "success": total - failed,
        "failed": failed,
        "success_rate": round((total - failed) / total * 100, 1) if total else 0.0,
    }


def _group_count(start: date, end: date, column, limit: int = 8) -> list[dict]:
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
    return [{"name": name, "count": count} for name, count in rows]


# 系统状态缓存：目录遍历有成本，30 秒内复用同一份结果。
# 数据还空着时只缓存 5 秒——刚灌完第一批记录就刷新页面，
# 不该看到"图片 0 张"这种明显过期的数字。
_STATUS_CACHE: dict = {"at": 0.0, "ttl": 0.0, "data": None}
_STATUS_TTL = 30.0
_STATUS_TTL_EMPTY = 5.0


def _dir_usage(root: Path) -> tuple[int, int]:
    """递归统计文件数与总字节，坏掉的软链/权限错误直接跳过"""
    files = total = 0
    if not root.exists():
        return 0, 0
    for entry in root.rglob("*"):
        try:
            if entry.is_file():
                files += 1
                total += entry.stat().st_size
        except OSError:
            continue
    return files, total


def _human_age(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))} 分钟前"
    if hours < 24:
        return f"{int(hours)} 小时前"
    return f"{int(hours // 24)} 天前"


def _system_status() -> dict:
    """仪表盘底部那条"系统自己好不好"。

    业务指标再漂亮，备份挂了、日报没推、磁盘塞满也是白搭——
    这三件事以前只能 SSH 上去看，现在一眼可见。
    """
    now = time.monotonic()
    if _STATUS_CACHE["data"] is not None and now - _STATUS_CACHE["at"] < _STATUS_CACHE["ttl"]:
        return _STATUS_CACHE["data"]

    from ..config import BACKUP_DIR, DATA_DIR, IMAGES_DIR

    backups = sorted(BACKUP_DIR.glob("*.db.gz"), reverse=True) if BACKUP_DIR.exists() else []
    last_backup = None
    if backups:
        latest = backups[0]
        try:
            mtime = datetime.fromtimestamp(latest.stat().st_mtime)
            age = (datetime.now() - mtime).total_seconds() / 3600
            last_backup = {
                "name": latest.name,
                "at_text": mtime.strftime("%m-%d %H:%M"),
                "age_text": _human_age(age),
                "stale": age > 36,  # 每天一备，超过 36 小时没备上就该看
                "count": len(backups),
            }
        except OSError:
            last_backup = None

    yesterday = date.today() - timedelta(days=1)
    status_file = DATA_DIR / "reports" / f"{yesterday:%Y-%m-%d}.status.json"
    yesterday_push = None
    if status_file.exists():
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
            yesterday_push = {
                "date": yesterday.isoformat(),
                "pushed": bool(payload.get("pushed")),
                "message": (payload.get("message") or "")[:80],
            }
        except (OSError, ValueError):
            yesterday_push = None

    image_count, image_bytes = _dir_usage(IMAGES_DIR)

    data = {
        "last_backup": last_backup,
        "yesterday_push": yesterday_push,
        "image_count": image_count,
        "image_bytes": image_bytes,
    }
    ttl = _STATUS_TTL if (image_count or backups) else _STATUS_TTL_EMPTY
    _STATUS_CACHE.update(at=now, ttl=ttl, data=data)
    return data


@bp.get("/")
def index():
    return redirect(url_for("web.dashboard"))


# ========== 登录 ==========
@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(url_for("web.dashboard"))

    # 按来源 IP 计数：同一个出口后面坐一整层楼时，5 次/5 分钟仍然够用
    limit_key = f"login:{request.remote_addr or 'unknown'}"

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        allowed, retry_after = rate_hit(limit_key, LOGIN_MAX_FAILS, LOGIN_WINDOW_S)
        if not allowed:
            logger.warning("登录尝试过于频繁 ip=%s username=%s", request.remote_addr, username)
            log_action("login_blocked", detail=f"username={username} retry_after={retry_after}s", actor=username or "-")
            resp = make_response(
                render_template(
                    "login.html",
                    username=username,
                    locked_message=f"尝试次数过多，请 {retry_after} 秒后再试",
                ),
                429,
            )
            resp.headers["Retry-After"] = str(retry_after)
            return resp

        user = AdminUser.query.filter_by(username=username, is_active=True).first()
        # 用户名不存在也走一次 hash 校验，避免响应时间泄露账号是否存在
        if user is None or not user.check_password(password):
            logger.warning("登录失败 username=%s ip=%s", username, request.remote_addr)
            log_action("login_failed", detail=f"username={username}", actor=username or "-")
            flash("用户名或密码错误", "error")
            return render_template("login.html", username=username), 401

        session.clear()
        session["uid"] = user.id
        session.permanent = True
        user.last_login_at = datetime.utcnow()
        db.session.commit()
        rate_reset(limit_key)
        logger.info("登录成功 username=%s ip=%s", username, request.remote_addr)
        log_action("login", detail=f"username={username} role={user.role}", actor=username)

        next_url = request.args.get("next") or ""
        # 只允许站内相对路径跳转，防开放重定向
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("web.dashboard"))

    return render_template("login.html", username="", locked_message=None)


@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


# ========== 仪表盘 ==========
@bp.get("/dashboard")
@login_required
def dashboard():
    today = date.today()
    start = today - timedelta(days=6)

    trend_rows = (
        db.session.query(Record.work_date, func.count(Record.id))
        .filter(
            Record.is_deleted.is_(False),
            Record.work_date >= start,
            Record.work_date <= today,
        )
        .group_by(Record.work_date)
        .all()
    )
    counted = {d: c for d, c in trend_rows}
    series, cursor = [], start
    while cursor <= today:
        series.append({"date": cursor, "count": counted.get(cursor, 0)})
        cursor += timedelta(days=1)

    peak = max((p["count"] for p in series), default=0) or 1

    return render_template(
        "dashboard.html",
        today=today,
        today_stats=_totals(today, today),
        week_stats=_totals(start, today),
        series=series,
        peak=peak,
        by_employee=_group_count(today, today, Record.employee_name),
        by_template=_group_count(today, today, Record.template_name),
        by_partner=_group_count(today, today, Record.partner_name),
        recent=Record.query.filter(Record.is_deleted.is_(False))
        .order_by(Record.created_at.desc())
        .limit(8)
        .all(),
        system=_system_status(),
        yesterday=today - timedelta(days=1),
    )


# ========== 记录列表 ==========
@bp.get("/records")
@login_required
def records():
    today = date.today()
    start = _parse_date(request.args.get("start"), today - timedelta(days=6))
    end = _parse_date(request.args.get("end"), today)
    if start > end:
        start, end = end, start
    query = Record.query.filter(
        Record.is_deleted.is_(False),
        Record.work_date >= start,
        Record.work_date <= end,
    )

    employee = request.args.get("employee", "")
    partner = request.args.get("partner", "")
    status = request.args.get("status", "")
    keyword = (request.args.get("q") or "").strip()

    if employee:
        query = query.filter(Record.employee_name == employee)
    if partner:
        query = query.filter(Record.partner_name == partner)
    if status:
        query = query.filter(Record.status == status)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            db.or_(
                Record.author.like(like),
                Record.title.like(like),
                Record.partner_name.like(like),
                Record.template_name.like(like),
            )
        )

    try:
        page = max(1, int(request.args.get("page", 1)))
        size = 20
    except ValueError:
        page = 1

    total = query.count()
    pages = max(1, (total + size - 1) // size)
    # 越界页码夹回最后一页：否则 ?page=999999 会让 SQLite 扫完整个结果集
    # 只为了返回空列表，页头还会显示 "999999 / 2" 这种鬼东西
    page = min(page, pages)
    rows = (
        query.order_by(Record.created_at.desc(), Record.id.desc())
        .limit(size)
        .offset((page - 1) * size)
        .all()
    )

    def distinct(column):
        result = (
            db.session.query(column)
            .filter(Record.is_deleted.is_(False), column.isnot(None), column != "")
            .distinct()
            .order_by(column)
            .all()
        )
        return [r[0] for r in result]

    # 回收站：与主表同一日期区间内的软删除记录（恢复入口）
    deleted_rows = (
        Record.query.filter(
            Record.is_deleted.is_(True),
            Record.work_date >= start,
            Record.work_date <= end,
        )
        .order_by(Record.deleted_at.desc().nullslast(), Record.id.desc())
        .limit(50)
        .all()
    )
    deleted_total = Record.query.filter(Record.is_deleted.is_(True)).count()

    # 快捷日期区间：与当前 start/end 精确匹配的标记为激活态
    quick_ranges = [
        {"label": "今天", "start": today, "end": today, "active": False},
        {"label": "近 7 天", "start": today - timedelta(days=6), "end": today, "active": False},
        {"label": "本月", "start": today.replace(day=1), "end": today, "active": False},
        {"label": "全部", "start": date(2000, 1, 1), "end": today, "active": False},
    ]
    for item in quick_ranges:
        item["active"] = start == item["start"] and end == item["end"]

    # 只收非空筛选值：分页链接、快捷日期、导出链接都复用，避免 URL 里挂一堆 employee=&q=
    filter_args = {
        key: value
        for key in ("employee", "partner", "status", "q")
        if (value := (request.args.get(key) or "").strip())
    }

    return render_template(
        "records.html",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        start=start,
        end=end,
        employee=employee,
        partner=partner,
        status=status,
        q=keyword,
        employees=distinct(Record.employee_name),
        partners=distinct(Record.partner_name),
        deleted_rows=deleted_rows,
        deleted_total=deleted_total,
        quick_ranges=quick_ranges,
        filter_args=filter_args,
        today=today,
    )


# ========== 日报预览 ==========
@bp.get("/reports")
@login_required
def reports():
    """展示最近生成的日报（bot 写入 report 目录）"""
    import json

    from ..config import DATA_DIR, Config

    report_dir = DATA_DIR / "reports"
    files = sorted(report_dir.glob("*.md"), reverse=True)[:30] if report_dir.exists() else []

    # 每条日报的推送状态（bot/report.save_push_status 落盘的同名 JSON）
    statuses: dict[str, dict] = {}
    for f in files:
        status_file = report_dir / f"{f.stem}.status.json"
        try:
            statuses[f.stem] = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass  # 老日报没有状态文件，不显示徽标

    selected = request.args.get("file", "")
    content = ""
    if selected:
        candidate = report_dir / selected
        # 防目录穿越：只允许 report_dir 下的 .md
        if candidate.suffix == ".md" and candidate.parent.resolve() == report_dir.resolve():
            if candidate.exists():
                content = candidate.read_text(encoding="utf-8")
    elif files:
        selected = files[0].name
        content = files[0].read_text(encoding="utf-8")

    # 重推时用文件名反解日期，解不出来就不给按钮——比让接口悄悄改成
    # "今天就今天"强，那样会把错误的日期推给全公司
    selected_date = None
    if selected:
        try:
            selected_date = datetime.strptime(Path(selected).stem, "%Y-%m-%d").date()
        except ValueError:
            selected_date = None

    return render_template(
        "reports.html",
        files=[f.name for f in files],
        selected=selected,
        selected_date=selected_date,
        content=content,
        statuses=statuses,
        report_schedule="%02d:%02d" % (Config.DAILY_REPORT_HOUR, Config.DAILY_REPORT_MINUTE),
    )


# ========== 操作日志 ==========
@bp.get("/audit")
@login_required
def audit():
    """后台写操作的审计流水（删除/恢复/导出/重推/登录）"""
    from ..models import AuditLog

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    action = (request.args.get("action") or "").strip()

    query = AuditLog.query
    if action:
        query = query.filter(AuditLog.action == action)

    size = 50
    total = query.count()
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    rows = (
        query.order_by(AuditLog.id.desc())
        .limit(size)
        .offset((page - 1) * size)
        .all()
    )

    actions = [
        {"value": value, "label": ACTION_LABELS.get(value, value)}
        for (value,) in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action).all()
    ]

    return render_template(
        "audit.html",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        action=action,
        actions=actions,
        labels=ACTION_LABELS,
    )
