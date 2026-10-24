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
    # 回调鉴权缺失时启动就喊一声：这类"配置漏了但服务正常"的洞，
    # 只写在文档里等于没有
    bot.warn_if_insecure()

    from ..web.views import bp as web_bp

    app.register_blueprint(web_bp)
