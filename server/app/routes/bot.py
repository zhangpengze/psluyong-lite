"""QQ 机器人回调：NapCat 反向上报入口。

NapCat「网络配置」里配置 HTTP 上报地址指向本路由后，群里每一条消息
都会 POST 过来。这里立即返回 200，实际处理放后台线程——
上报同步等待会把 NapCat 侧拖到超时重试，同一条消息重复处理。

鉴权（2026-10-24 收紧，原为"没配密钥就放行"）：
    - BOT_CALLBACK_SECRET 配了：请求头 X-Bot-Secret 或 Bearer 必须一致
    - 没配：**默认拒绝**（fail-closed）。这个端点一旦匿名可达，任何能碰到
      NAS 端口的人 POST 一条「汇总」就能拿到含员工姓名/作者/标题/失败原因的
      报表。只有显式设 BOT_ALLOW_INSECURE=1（完全隔离的内网/本机调试）才放行。

过滤顺序也调整过：先看 post_type/message_type 再起线程。NapCat 上报量里
心跳与通知占大头，旧实现给每一条都开一个线程，等于把过滤成本放在最贵的
地方——线程耗尽时 Thread.start() 抛错，NapCat 侧还会重试，自放大。
"""

from __future__ import annotations

import hmac
import logging
import threading

from flask import Blueprint, current_app, jsonify, request

from server.app.config import Config
from server.app.ratelimit import hit as rate_hit

bp = Blueprint("bot_api", __name__, url_prefix="/api/v1/bot")
logger = logging.getLogger(__name__)

# 上报限流：NapCat 正常流量是"群里说话才有上报"，每分钟几十条已经很多。
# 不限流时一个死循环脚本就能把进程线程刷干。
BOT_RATE_LIMIT = 120
BOT_RATE_WINDOW_S = 60


def _secret_ok() -> bool:
    secret = (Config.BOT_CALLBACK_SECRET or "").strip()
    if not secret:
        # fail-closed：没配密钥就不处理。要让老部署继续用，必须显式
        # BOT_ALLOW_INSECURE=1——"忘了配"和"故意不配"必须能区分开。
        return bool(Config.BOT_ALLOW_INSECURE)
    provided = (request.headers.get("X-Bot-Secret") or "").strip()
    if not provided:
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    # 常量时间比较（同 auth.check_api_key），避免按响应时间逐字节猜密钥。
    # 转 utf-8 bytes 再比：compare_digest 对含非 ASCII 的 str 会抛 TypeError，
    # 攻击者发个中文头就能把 403 变成 500。
    return hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8"))


def _is_group_message(payload: dict) -> bool:
    """廉价前置过滤：心跳/通知/私聊在进线程之前就挡掉"""
    return payload.get("post_type") == "message" and payload.get("message_type") == "group"


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


def warn_if_insecure() -> None:
    """启动时把"回调等于半开放"的事实喊出来，别让它只写在文档里。

    三种情况都要能一眼看到：完全没配密钥（最危险）、放行了无密钥回调、
    白名单为空（谁能触发是不确定的）。
    """
    if not Config.BOT_CALLBACK_SECRET:
        if Config.BOT_ALLOW_INSECURE:
            logger.warning(
                "机器人回调未设密钥且 BOT_ALLOW_INSECURE=1：任何能访问本端口的程序"
                "都能触发指令、拿到含员工姓名与稿件标题的报表。仅限完全隔离的内网。"
            )
        else:
            logger.info("机器人回调未设 BOT_CALLBACK_SECRET：/api/v1/bot/onebot 已拒绝全部上报")
    if not Config.BOT_ADMIN_QQ_IDS:
        logger.info("BOT_ADMIN_QQ_IDS 为空：群内指令谁都不能触发（需要显式列出 QQ 号）")
    if not Config.BOT_ALLOWED_GROUP_IDS and not Config.REPORT_GROUP_ID:
        logger.warning("BOT_ALLOWED_GROUP_IDS 与 REPORT_GROUP_ID 都为空：群内指令无法确定允许的群")


@bp.post("/onebot")
def onebot_callback():
    if not _secret_ok():
        logger.warning("机器人回调被拒绝（密钥缺失或不匹配）: remote=%s", request.remote_addr)
        return jsonify({"status": "forbidden"}), 403

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        # NapCat 可能发空体/心跳，直接吞掉
        return jsonify({"status": "ok", "handled": False})

    if not _is_group_message(payload):
        # 心跳、通知、私聊：不进线程
        return jsonify({"status": "ok", "handled": False})

    allowed, retry_after = rate_hit(
        f"bot:{request.remote_addr}", BOT_RATE_LIMIT, BOT_RATE_WINDOW_S
    )
    if not allowed:
        logger.warning(
            "机器人回调超限（%s 次/%ss）: remote=%s",
            BOT_RATE_LIMIT,
            BOT_RATE_WINDOW_S,
            request.remote_addr,
        )
        return jsonify({"status": "rate_limited", "retry_after": retry_after}), 429

    # current_app 是线程局部的，必须取真实对象带进线程
    _spawn(current_app._get_current_object(), payload)
    return jsonify({"status": "ok"})
