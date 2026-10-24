"""后台页面（服务端渲染）

只做三件事：解析请求参数 → 调 service 取数 → 组装模板/跳转。
业务逻辑（口令校验、审计查询、日报文件读取）全在 services/。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

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
from ..auth import current_user, generate_csrf_token, login_required
from ..config import Config
from ..overview import RangeError, get_overview, parse_limit, parse_range
from ..panels import overview_html
from ..ratelimit import hit as rate_hit
from ..ratelimit import reset as rate_reset
from ..services import audit_service, auth_admin, records_read, records_service, report_read

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

        user = auth_admin.authenticate(username, password)
        if user is None:
            logger.warning("登录失败 username=%s ip=%s", username, request.remote_addr)
            log_action("login_failed", detail=f"username={username}", actor=username or "-")
            flash("用户名或密码错误", "error")
            return render_template("login.html", username=username), 401

        session.clear()
        session["uid"] = user.id
        session.permanent = True
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
    """记录列表：筛选 / 分页 / 回收站数据全部下沉 services，与 API 同一套语义。

    筛选关键词的 LIKE 转义、页码夹取等规则只在 services 有一份实现，
    后台页面与 /api/v1/records 不会再各写一遍然后漂移。
    """
    today = date.today()
    start, end = records_service.resolve_range(request.args.get("start"), request.args.get("end"))
    employee = request.args.get("employee", "")
    partner = request.args.get("partner", "")
    status = request.args.get("status", "")
    keyword = (request.args.get("q") or "").strip()

    query, _params = records_service.build_filtered_query(
        start, end, employee=employee, partner=partner, status=status, keyword=keyword
    )
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    rows, total, page, pages = records_read.paginate(query, page, 20)

    facets = records_service.facet_values("employees", "partners")
    # 回收站：与主表同一日期区间内的软删除记录（恢复入口）
    deleted_rows = records_read.list_deleted_rows(start, end)
    deleted_total = records_read.count_deleted()

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
        employees=facets["employees"],
        partners=facets["partners"],
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
    """展示最近生成的日报（bot 写入 report 目录）。取数与路径校验在 report_read。"""
    files = report_read.list_reports()
    statuses = report_read.load_statuses(files)
    selected, content = report_read.resolve_content(request.args.get("file", ""), files)
    selected_date = report_read.parse_report_date(selected)

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
    """后台写操作的审计流水（删除/恢复/导出/重推/登录）。取数在 audit_service。"""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    action = (request.args.get("action") or "").strip()

    rows, total, page, pages = audit_service.list_entries(action, page)
    actions = [
        {"value": value, "label": ACTION_LABELS.get(value, value)}
        for value in audit_service.action_values()
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
