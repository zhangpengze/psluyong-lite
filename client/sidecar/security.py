"""sidecar 的 HTTP 安全层（拆自 server.py）：CORS 白名单、跨站写拦截、413、OPTIONS 预检。

这些都是 app 级钩子（errorhandler / before / after request），与具体业务路由无关；
放在一起能一眼看清"只绑 127.0.0.1 的本机服务"的信任边界在哪。
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

# CORS 白名单：Tauri 生产环境是 tauri://localhost，开发是 vite 的 5173
ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
}


def register_security(app: Flask) -> None:
    """挂安全钩子。必须先于业务路由注册（保持原 server.py 的注册顺序）。"""
    @app.errorhandler(413)
    def too_large(_err):
        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return jsonify({"ok": False, "error": f"文件太大，单次上传合计上限 {limit} MB"}), 413

    @app.after_request
    def cors(resp):
        origin = request.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        return resp

    @app.before_request
    def reject_cross_site_writes():
        """写操作自己判来源。

        CORS 只约束"能不能读响应"，约束不了"请求是否被执行"——恶意网页用
        form 提交（无需预检）照样能让本机 sidecar 改配置、打开目录。
        带 Origin 的写请求必须在白名单里；不带 Origin 的是本机程序（curl、
        PowerShell、Tauri 内部），放行。
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        origin = request.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            logger.warning("拒绝跨站写请求 origin=%s path=%s", origin, request.path)
            return jsonify({"ok": False, "error": "请求来源不被允许"}), 403
        return None

    @app.route("/<path:_any>", methods=["OPTIONS"])
    def preflight(_any):
        return ("", 204)
