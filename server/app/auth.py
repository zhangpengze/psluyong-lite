"""
认证

两套凭据，各管一条链路：
  - API Key：员工客户端调接口（机器身份，长期有效）
  - 会话 Cookie：主管后台（人身份，12 小时过期）

刻意不引入 JWT：后台是服务端渲染，浏览器里用 session 更简单，
也少一个依赖和一类"token 放哪"的安全问题。
"""

from __future__ import annotations

import hmac
import logging
from functools import wraps

from flask import g, jsonify, redirect, request, session, url_for

from .config import Config
from .models import AdminUser

logger = logging.getLogger(__name__)


def _unauthorized(message: str, code: int = 401):
    return jsonify({"ok": False, "error": message}), code


def check_api_key(candidate: str) -> bool:
    """常量时间比较，避免按响应时间逐字节猜 key"""
    return bool(candidate) and hmac.compare_digest(candidate, Config.API_KEY)


def api_key_required(view):
    """API Key 校验（员工客户端）"""

    @wraps(view)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if not check_api_key(key):
            logger.warning("API Key 校验失败 ip=%s path=%s", request.remote_addr, request.path)
            return _unauthorized("API Key 无效")
        return view(*args, **kwargs)

    return wrapper


def current_user() -> AdminUser | None:
    """当前登录的后台账号（同一次请求内只查一次）"""
    if "user" in g:
        return g.user
    user_id = session.get("uid")
    user = None
    if user_id:
        user = AdminUser.query.filter_by(id=user_id, is_active=True).first()
        if user is None:
            session.clear()
    g.user = user
    return user


def login_required(view):
    """后台页面：未登录跳登录页；API 返回 401"""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            if request.path.startswith("/api/"):
                return _unauthorized("请先登录")
            return redirect(url_for("web.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    """写操作（维护字典/模板）仅管理员"""

    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            if request.path.startswith("/api/"):
                return _unauthorized("请先登录")
            return redirect(url_for("web.login", next=request.full_path))
        if not user.is_admin:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "需要管理员权限"}), 403
            return "需要管理员权限", 403
        return view(*args, **kwargs)

    return wrapper


def api_or_session(view):
    """读接口：客户端用 API Key，后台页面用会话，两者都放行"""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if check_api_key(request.headers.get("X-API-Key", "")):
            g.auth_via = "api_key"
            return view(*args, **kwargs)
        if current_user() is not None:
            g.auth_via = "session"
            return view(*args, **kwargs)
        return _unauthorized("请先登录或提供 API Key")

    return wrapper
