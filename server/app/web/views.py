"""后台页面（服务端渲染）"""

from __future__ import annotations

import json
import logging
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

from ..audit import ACTION_LABELS, log_action
from ..auth import admin_required, current_user, generate_csrf_token, login_required
from ..db import db
from ..models import AdminUser, Record
from ..overview import RangeError, get_overview, parse_limit, parse_range
from ..panels import overview_html
from ..ratelimit import hit as rate_hit
from ..ratelimit import reset as rate_reset
from ..search import LIKE_ESCAPE, like_contains

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
    """数据概览：KPI + 趋势 + 合作伙伴明细，首屏服务端直出。

    时间范围来自 URL（range/start/end），与 /api/v1/stats/overview 同一套
    解析；非法区间不白屏，flash 提示后回退默认的「近 7 天」。
    """
    try:
        start, end, label, preset = parse_range(request.args)
    except RangeError as e:
        flash(str(e), "error")
        start, end, label, preset = parse_range({"range": "7d"})

    ov = dict(get_overview(start, end, label, preset))
    ov["html"] = overview_html(ov, parse_limit(request.args))
    return render_template("dashboard.html", ov=ov)


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
        # 必须走 like_contains + escape：直接 f"%{keyword}%" 会让 SQLite 把
        # 关键词里的 % 和 _ 当通配符，主管搜「50%」等于把全表捞出来当搜索结果。
        # 这条规则和 API 接口共用同一份实现（server/app/search.py）。
        like = like_contains(keyword)
        query = query.filter(
            db.or_(
                Record.author.like(like, escape=LIKE_ESCAPE),
                Record.title.like(like, escape=LIKE_ESCAPE),
                Record.partner_name.like(like, escape=LIKE_ESCAPE),
                Record.template_name.like(like, escape=LIKE_ESCAPE),
                # 员工名也要能搜：API 侧一直支持，后台页面原来漏了，
                # 同一个词在两边给出不同结果会让人怀疑数据。
                Record.employee_name.like(like, escape=LIKE_ESCAPE),
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
