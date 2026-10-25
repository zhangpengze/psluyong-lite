"""官方 QQ 机器人推送（QQ 开放平台 v2 OpenAPI）。

历史：这里以前走 NapCat——第三方协议端，登录一个**真 QQ 号**当机器人用。
2026-10 起整条链路换成官方机器人。换的原因不是技术偏好，是授权：
NapCat 的 README 明确写了"未经作者授权不得做衍生开发"，而我们把它打进
NAS 安装包＝在分发，这件事没有任何授权依据。官方通道唯一的硬限制是群主动
消息频率（已认证 60 条/分钟、1000 条/天/群），日报 + 告警一天不到 10 条。

三处和 OneBot 完全不同、最容易照着旧代码写错的地方：

  1. 鉴权头是 `Authorization: QQBot {access_token}`，**不是 Bearer**。
     token 2 小时过期，必须自己缓存并提前刷新（见 get_access_token）。
  2. 目标是 `group_openid`，不是群号。它是一串只能从事件里拿到的字符串
     （GROUP_AT_MESSAGE_CREATE / GROUP_ADD_ROBOT），把 123456789 填进来
     只会拿到 40054002。
  3. **HTTP 200 不等于发出去了**：失败时也是 200，靠响应体里的 `err_code`。
     和以前 OneBot 的 `{"status":"failed"}` 是同一个坑，见 _judge()。

官方域名变更记录：2026-08-10 起旧域名 api.sgroup.qq.com 已下线，所有接口
统一到 https://api.bot.qq.com（取 token 也在同一个域名下）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import quote

from .qqbot_errors import code_of, explain

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.bot.qq.com"
TOKEN_PATH = "/app/getAppAccessToken"

# 推送重试：网络抖一下/对方 5xx 时当天不丢。间隔按次递增，测试里 monkeypatch 成 (0, 0)。
PUSH_ATTEMPTS = 3
PUSH_BACKOFF_S = (5, 15)

REQUEST_TIMEOUT_S = 10
# 文档：在上一个 token 接近过期 60 秒内再取会拿到新的，老的这 60 秒仍有效。
# 所以"提前量"取 60——卡着点换，等于赌最后一秒的网络没问题。
TOKEN_REFRESH_MARGIN_S = 60
MSG_TYPE_TEXT = 0

_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {"token": "", "appid": "", "expires_at": 0.0}


def reset_token_cache() -> None:
    """测试用：丢掉缓存。用例之间不能互相借 token，否则"提前刷新"永远测不到。"""
    with _token_lock:
        _token_cache.update({"token": "", "appid": "", "expires_at": 0.0})


def api_base() -> str:
    """读 Config 而不是模块常量：测试 / 私有网关要能改，且必须每次现读"""
    from server.app.config import Config

    return (getattr(Config, "QQ_BOT_API_BASE", "") or DEFAULT_API_BASE).rstrip("/")


def config_problem(need_group: bool = True) -> str:
    """返回""表示配置齐全；否则返回**缺什么**（不带"跳过"之类的结论）。

    只回"缺哪一项"而不是一句完整的话：仪表盘要把它塞进括号里当原因，
    推送结果要拼上"已跳过推送"，两处措辞不一样，让调用方各拼各的。
    """
    from server.app.config import Config

    if not (Config.QQ_BOT_APPID or "").strip() or not (Config.QQ_BOT_SECRET or "").strip():
        return "未配置 QQ_BOT_APPID / QQ_BOT_SECRET"
    if need_group and not (Config.QQ_BOT_GROUP_OPENID or "").strip():
        return "缺少 QQ_BOT_GROUP_OPENID"
    return ""


def auth_headers(token: str) -> dict[str, str]:
    # 前缀是 QQBot 不是 Bearer。写成 Bearer 一律 401，且日志里看不出原因。
    return {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}


def _payload_of(resp) -> dict:
    """取响应体 JSON；拿不到/不是对象就返回空 dict（绝不抛给调用方）"""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - 反代错误页 / 空 body
        return {}
    return body if isinstance(body, dict) else {}


def get_access_token(force: bool = False) -> tuple[str, str]:
    """取 access_token，成功返回 (token, "")，失败返回 ("", 原因)。

    缓存两个理由：token 2 小时有效，每次推送都去换会被平台限流；而且
    频繁换 token 在日志里看着像"鉴权一直在失败"，排查时误导人。
    """
    from server.app.config import Config

    appid = (Config.QQ_BOT_APPID or "").strip()
    secret = (Config.QQ_BOT_SECRET or "").strip()
    if not appid or not secret:
        return "", "未配置 QQ_BOT_APPID / QQ_BOT_SECRET"

    with _token_lock:
        if (
            not force
            and _token_cache["token"]
            and _token_cache["appid"] == appid
            and time.monotonic() < _token_cache["expires_at"]
        ):
            return str(_token_cache["token"]), ""

    import requests

    try:
        resp = requests.post(
            f"{api_base()}{TOKEN_PATH}",
            json={"appId": appid, "clientSecret": secret},
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.RequestException as e:
        return "", f"取 access_token 异常: {e}"

    body = _payload_of(resp)
    token = str(body.get("access_token") or "")
    if getattr(resp, "status_code", 0) != 200 or not token:
        code = code_of(body)
        detail = explain(code if code is not None else getattr(resp, "status_code", 0),
                         str(body.get("message") or ""))
        return "", f"取 access_token 失败: {detail}"

    try:
        # 文档示例里 expires_in 是**字符串** "7200"，不是数字——直接当 int 用会炸
        ttl = int(float(body.get("expires_in") or 7200))
    except (TypeError, ValueError):
        ttl = 7200
    with _token_lock:
        _token_cache.update(
            {
                "token": token,
                "appid": appid,
                "expires_at": time.monotonic() + max(0, ttl - TOKEN_REFRESH_MARGIN_S),
            }
        )
    return token, ""


def _judge(resp) -> tuple[bool, str, bool]:
    """(成功?, 说明, 是否值得重试)。

    HTTP 200 也要看 err_code——"日志绿、徽标绿、群里没消息"就是这么来的：
    群没开主动消息权限时平台回 200 + err_code=40034102，只看状态码的话
    pushed 会被记成 True，而"定时日报未推送成功"这条兜底告警的判据正是
    pushed 为假，于是整套告警一起失灵。
    """
    status = int(getattr(resp, "status_code", 0) or 0)
    body = _payload_of(resp)
    raw = str(getattr(resp, "text", "") or "")[:200]

    if status == 200:
        code = code_of(body)
        if code in (0, None):
            return True, "推送成功", False
        return False, explain(code, str(body.get("message") or "")), False

    if status == 401:
        # token 被提前失效（平台侧轮换 / 换了 AppSecret）。清缓存，让下一轮重新取。
        reset_token_cache()
        return False, "access_token 无效或已过期（HTTP 401），检查 QQ_BOT_APPID / QQ_BOT_SECRET", True
    if status == 429:
        return False, explain(11244), False
    if 400 <= status < 500:
        return False, f"推送被拒绝 HTTP {status}（请求被拒绝，不重试）：{raw}", False
    return False, f"推送失败 HTTP {status}：{raw}", True


def push_to_group(content: str, group_id=None, description: str = "日报推送") -> tuple[bool, str]:
    """把纯文本发到群。未配置视为"跳过"而非失败；失败按 5s/15s 退避重试 3 次。

    group_id 允许传别的群的 openid，正常路径永远用 QQ_BOT_GROUP_OPENID——
    绝不能借默认值把报表发到别的群去。
    """
    from server.app.config import Config

    problem = config_problem(need_group=group_id is None)
    if problem:
        logger.info("%s，跳过%s（日报已存档）", problem, description)
        return False, f"{problem}，已跳过推送"

    target = str(group_id if group_id is not None else Config.QQ_BOT_GROUP_OPENID).strip()
    if not target:
        return False, "缺少 QQ_BOT_GROUP_OPENID，已跳过推送"

    # 发送前最后一道：超长消息平台直接拒收（err_code 40034xxx），
    # 群里没消息而"推送成功"的假象只能靠先截断来避免。
    # 截的是发送副本，存档的 .md 仍是全文，尾部那句"完整版见后台"才成立。
    from .report_render import clamp_for_message

    text, dropped = clamp_for_message(content)
    if dropped:
        logger.warning("%s内容超长，已省略 %s 行（完整版见后台「每日日报」存档）", description, dropped)

    import requests

    url = f"{api_base()}/v2/groups/{quote(target, safe='')}/messages"
    # 不带 msg_id = 主动消息；不带 msg_seq = 默认 1。故意不自己编 msg_seq：
    # 重试时复用同一个值可能被判重复（40054005），换一个新值又会让平台
    # 当成两条不同消息——重试那点极小概率的重复，比这两头都轻。
    payload = {"content": text, "msg_type": MSG_TYPE_TEXT}

    last = "未知错误"
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        token, token_err = get_access_token()
        if not token:
            last = f"鉴权失败: {token_err}"
            logger.error(
                "第 %d/%d 次%s无法取到 access_token: %s", attempt, PUSH_ATTEMPTS, description, token_err
            )
        else:
            try:
                resp = requests.post(
                    url, json=payload, headers=auth_headers(token), timeout=REQUEST_TIMEOUT_S
                )
            except requests.RequestException as e:
                last = f"推送异常: {e}"
                logger.error("第 %d/%d 次%s异常: %s", attempt, PUSH_ATTEMPTS, description, e)
            else:
                ok, detail, retry = _judge(resp)
                if ok:
                    logger.info("%s成功（群 %s）", description, target)
                    return True, "推送成功"
                last = detail
                if retry:
                    logger.error("第 %d/%d 次%s失败: %s", attempt, PUSH_ATTEMPTS, description, detail)
                else:
                    logger.error("%s失败（不重试）: %s", description, detail)
                    return False, detail

        if attempt < PUSH_ATTEMPTS:
            wait = PUSH_BACKOFF_S[min(attempt - 1, len(PUSH_BACKOFF_S) - 1)]
            logger.warning("%ds 后重试%s", wait, description)
            time.sleep(wait)
    return False, f"{last}（已重试 {PUSH_ATTEMPTS} 次）"


__all__ = [
    "DEFAULT_API_BASE",
    "MSG_TYPE_TEXT",
    "PUSH_ATTEMPTS",
    "PUSH_BACKOFF_S",
    "REQUEST_TIMEOUT_S",
    "TOKEN_REFRESH_MARGIN_S",
    "api_base",
    "auth_headers",
    "config_problem",
    "get_access_token",
    "push_to_group",
    "reset_token_cache",
]
