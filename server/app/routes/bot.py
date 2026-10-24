"""QQ 机器人回调：NapCat 反向上报入口。

NapCat「网络配置」里配置 HTTP 上报地址指向本路由后，群里每一条消息
都会 POST 过来。这里立即返回 200，实际处理放后台线程——
上报同步等待会把 NapCat 侧拖到超时重试，同一条消息重复处理。

鉴权：NapCat 上报不带后台 session，无法走 api_or_session。
可选的 BOT_CALLBACK_SECRET 配了才校验（请求头 X-Bot-Secret 或
Authorization: Bearer），没配则放行——内网部署场景，默认信任网络边界。
"""

from __future__ import annotations

import hmac
import logging
import threading

from flask import Blueprint, current_app, jsonify, request

from server.app.config import Config

bp = Blueprint("bot_api", __name__, url_prefix="/api/v1/bot")
logger = logging.getLogger(__name__)


def _secret_ok() -> bool:
    secret = (Config.BOT_CALLBACK_SECRET or "").strip()
    if not secret:
        return True
    provided = (request.headers.get("X-Bot-Secret") or "").strip()
    if not provided:
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    # 常量时间比较（同 auth.check_api_key），避免按响应时间逐字节猜密钥。
    # 转 utf-8 bytes 再比：compare_digest 对含非 ASCII 的 str 会抛 TypeError，
    # 攻击者发个中文头就能把 403 变成 500。secret 为空仍直接放行（内网信任边界，见模块注释）。
    return hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8"))


def _handle_async(app, payload: dict) -> None:
    """后台线程里跑 handler，任何异常都不能逃出线程变成静默死亡。

    包 app_context：handler 里的审计写入（log_action 要写数据库会话）
    需要应用上下文，而后台线程没有。
    """
    from bot.handler import handle_event

    try:
        with app.app_context():
            replied, message = handle_event(app, payload)
        if replied:
            logger.info("机器人回调处理完成: %s", message)
    except Exception:  # noqa: BLE001 - 后台线程必须吞掉异常并留日志
        logger.exception("机器人回调处理异常")


def _spawn(app, payload: dict) -> None:
    """起后台线程处理。测试里可 monkeypatch 成同步执行。"""
    threading.Thread(target=_handle_async, args=(app, payload), daemon=True).start()


@bp.post("/onebot")
def onebot_callback():
    if not _secret_ok():
        logger.warning("机器人回调密钥校验失败: remote=%s", request.remote_addr)
        return jsonify({"status": "forbidden"}), 403

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        # NapCat 可能发空体/心跳，直接吞掉
        return jsonify({"status": "ok", "handled": False})

    # current_app 是线程局部的，必须取真实对象带进线程
    _spawn(current_app._get_current_object(), payload)
    return jsonify({"status": "ok"})
