"""健康检查"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from ..services import health_service

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    """存活探针：只证明进程还在"""
    return jsonify({"ok": True, "status": "alive"})


@bp.get("/health/ready")
def ready():
    """就绪探针：真连一次数据库，连不上就别把流量放进来"""
    try:
        health_service.ping_database()
        return jsonify({"ok": True, "status": "ready", "db": "ok"})
    except Exception as e:  # noqa: BLE001
        # 异常原文（表名、路径、SQL）只进日志，不回给调用方
        current_app.logger.exception("就绪探针失败: %s", e)
        return jsonify({"ok": False, "status": "not_ready", "db": "error"}), 503
