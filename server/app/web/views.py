"""后台页面（服务端渲染）"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func

from ..auth import admin_required, current_user, login_required
from ..db import db
from ..models import STATUS_FAILED, AdminUser, Record

bp = Blueprint("web", __name__)
logger = logging.getLogger(__name__)


@bp.app_context_processor
def inject_current_user():
    """所有模板都能拿到当前账号，用于控制"删除"这类仅管理员可见的按钮"""
    return {"me": current_user()}


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


@bp.get("/")
def index():
    return redirect(url_for("web.dashboard"))


# ========== 登录 ==========
@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(url_for("web.dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = AdminUser.query.filter_by(username=username, is_active=True).first()
        # 用户名不存在也走一次 hash 校验，避免响应时间泄露账号是否存在
        if user is None or not user.check_password(password):
            logger.warning("登录失败 username=%s ip=%s", username, request.remote_addr)
            flash("用户名或密码错误", "error")
            return render_template("login.html", username=username), 401

        session.clear()
        session["uid"] = user.id
        session.permanent = True
        user.last_login_at = datetime.utcnow()
        db.session.commit()
        logger.info("登录成功 username=%s ip=%s", username, request.remote_addr)

        next_url = request.args.get("next") or ""
        # 只允许站内相对路径跳转，防开放重定向
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("web.dashboard"))

    return render_template("login.html", username="")


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

    return render_template(
        "records.html",
        rows=rows,
        total=total,
        page=page,
        pages=(total + size - 1) // size,
        start=start,
        end=end,
        employee=employee,
        partner=partner,
        status=status,
        q=keyword,
        employees=distinct(Record.employee_name),
        partners=distinct(Record.partner_name),
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

    return render_template(
        "reports.html",
        files=[f.name for f in files],
        selected=selected,
        content=content,
        statuses=statuses,
        report_schedule="%02d:%02d" % (Config.DAILY_REPORT_HOUR, Config.DAILY_REPORT_MINUTE),
    )
