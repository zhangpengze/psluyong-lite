"""应用工厂"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

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
    _register_cli(app)
    return app


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
