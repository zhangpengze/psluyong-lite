"""蓝图注册"""

from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from . import bot, health, records, reports, stats

    app.register_blueprint(health.bp)
    app.register_blueprint(records.bp)
    app.register_blueprint(stats.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(bot.bp)

    from ..web.views import bp as web_bp

    app.register_blueprint(web_bp)
