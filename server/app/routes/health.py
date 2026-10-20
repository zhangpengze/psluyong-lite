"""健康检查"""

from __future__ import annotations

from flask import Blueprint, jsonify
from sqlalchemy import text

from ..db import db

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    """存活探针：只证明进程还在"""
    return jsonify({"ok": True, "status": "alive"})


@bp.get("/health/ready")
def ready():
    """就绪探针：真连一次数据库，连不上就别把流量放进来"""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"ok": True, "status": "ready", "db": "ok"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "status": "not_ready", "db": str(e)}), 503
