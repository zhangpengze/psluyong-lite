"""健康检查"""

from __future__ import annotations

from flask import Blueprint, jsonify

from ..services import health_service

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    """存活探针：只证明进程还在"""
    return jsonify({"ok": True, "status": "alive"})


@bp.get("/health/ready")
def ready():
    """就绪探针：数据库能连、数据目录能写，才算真的能用。

    失败原因（路径、SQL、异常对象）只进日志，不回给调用方。
    """
    report = health_service.collect_readiness()
    return jsonify(report), (200 if report["ok"] else 503)
