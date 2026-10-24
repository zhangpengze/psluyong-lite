"""应用工厂"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from .config import Config, ensure_dirs
from .db import db


def create_app(config_object: type = Config) -> Flask:
    app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
    app.config.from_object(config_object)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    ensure_dirs()
    db.init_app(app)

    from .routes import register_blueprints

    register_blueprints(app)

    with app.app_context():
        from .migrations import init_database

        init_database(app)

    _register_filters(app)
    _register_error_handlers(app)
    _register_security_headers(app)
    _register_cli(app)
    return app


def _asset_version(app: Flask) -> str:
    """静态资源版本号：取 static 目录里最新 mtime 的短哈希。

    改完 app.css 直接刷新就能看到新样式，不用叮嘱用户 Ctrl+F5；
    内容没变时 URL 也不变，浏览器缓存继续命中。

    做成函数而不是启动时算一次的常量：开发时改了 CSS 不重启也能生效。
    static 目录只有几个文件，每次渲染的 stat 开销可以忽略。
    """
    static_dir = app.static_folder
    latest = 0.0
    if static_dir:
        for path in Path(static_dir).rglob("*"):
            if path.is_file():
                latest = max(latest, path.stat().st_mtime)
    return f"{latest:.0f}"


def _register_security_headers(app: Flask) -> None:
    """统一安全响应头。

    后台能被任意站点 iframe 套住 = 点击劫持；nosniff 防 MIME 嗅探把
    上传的图片当脚本执行。CSP 暂不下发——模板里仍有内联 script 与
    onclick，直接上 script-src 'self' 会整页白屏，等内联脚本外置后再加。
    """
    app.jinja_env.globals["asset_version"] = lambda: _asset_version(app)

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        # 静态资源文件名不稳定（app.css 不带 hash），靠 ?v= 做缓存击穿。
        # 这里必须显式赋值：Flask/Werkzeug 的 send_file 已先下发
        # Cache-Control: no-cache，setdefault 碰到既有值不会覆盖，实测拿不到
        # 7 天长缓存，表现为每次进后台都重拉 app.css。
        # 仅 /static/ 走覆盖，页面与 API 响应的缓存语义维持原样。
        if request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "public, max-age=604800"
        return resp


def _display_tz():
    """
    展示用时区。

    不能依赖进程本地时区：Windows 上 Python 会读 TZ 环境变量，而 .env 里的
    "Asia/Shanghai" 在缺少 tzdata 时会静默退化成 UTC，页面时间就整体偏 8 小时。
    这里显式用 zoneinfo，拿不到时才回退系统本地时区。
    """
    from zoneinfo import ZoneInfo

    from .config import Config

    try:
        return ZoneInfo(Config.TZ)
    except Exception:  # noqa: BLE001 - 缺 tzdata 时退回系统时区
        return None


def _register_filters(app: Flask) -> None:
    tz = _display_tz()

    @app.template_filter("localtime")
    def localtime(value: datetime | None, fmt: str = "%m-%d %H:%M") -> str:
        """库里存 UTC，页面按配置时区显示"""
        if not value:
            return "-"
        aware = value.replace(tzinfo=timezone.utc)
        local = aware.astimezone(tz) if tz is not None else aware.astimezone()
        return local.strftime(fmt)

    @app.template_filter("filesize")
    def filesize(value: int | None) -> str:
        if not value:
            return "-"
        size = float(value)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(413)
    def too_large(_e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "上传文件超过大小限制"}), 413
        return "上传文件超过大小限制", 413

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "接口不存在"}), 404
        return render_template("error.html", code=404, message="页面不存在"), 404

    @app.errorhandler(403)
    def forbidden(_e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "需要管理员权限"}), 403
        return render_template("error.html", code=403, message="当前账号没有这个操作权限"), 403

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception("未捕获异常: %s", e)
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "服务器内部错误"}), 500
        return render_template("error.html", code=500, message="服务器内部错误"), 500


def _register_cli(app: Flask) -> None:
    import click

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--role", default="viewer", type=click.Choice(["admin", "viewer"]))
    def create_user(username: str, password: str, role: str) -> None:
        """新建后台账号"""
        from .models import AdminUser

        if AdminUser.query.filter_by(username=username).first():
            click.echo(f"用户 {username} 已存在")
            return
        user = AdminUser(username=username, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"已创建 {username}（{role}）")

    @app.cli.command("reset-password")
    @click.option("--username", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def reset_password(username: str, password: str) -> None:
        """重置密码"""
        from .models import AdminUser

        user = AdminUser.query.filter_by(username=username).first()
        if not user:
            click.echo(f"用户 {username} 不存在")
            return
        user.set_password(password)
        db.session.commit()
        click.echo("密码已重置")
