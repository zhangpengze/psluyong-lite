"""
日报生成与推送

设计取舍：
  - 报告先落盘成 Markdown（data/reports/YYYY-MM-DD.md），再尝试推送。
    推送失败不影响存档，后台"每日日报"页仍能看到当天数据。
  - 推送走 NapCat / OneBot v11 的 send_group_msg HTTP 接口。
    未配置 NAPCAT_WEBHOOK 时只落盘 + 记日志，不报错——
  - 单日与区间（本周/本月）共用一套「_collect_stats + _render」：
    区间版只多一个「按日」段落，统计口径完全一致。

文件拆分（单文件 ≤300 行）：区间统计在 report_stats.py，落盘在 report_store.py，
告警与节流在 report_alert.py；本文件保留渲染、推送与 run_daily 编排——
测试以 bot.report 为 monkeypatch 面（PUSH_BACKOFF_S / push_to_group /
PARTNER_TEMPLATE_LIMIT 等），这些名字必须留在本模块。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from .report_alert import (
    OPS_ALERT_MIN_INTERVAL_S,
    push_alert,
    push_ops_alert,
    reset_alert_throttle,
    reset_ops_alert_throttle,
)
from .report_render import render
from .report_stats import _collect_stats, stats_fingerprint
from .report_store import load_push_status, save_push_status, save_report

logger = logging.getLogger(__name__)

# 推送重试：NapCat 抖动（重启中/网络抖一下）时当天不丢。
# 间隔按次递增，测试里可 monkeypatch 成 (0, 0)。
PUSH_ATTEMPTS = 3
PUSH_BACKOFF_S = (5, 15)


# 每个合作伙伴下面最多展开几个模板，防止某个伙伴模板太多把日报撑爆
PARTNER_TEMPLATE_LIMIT = 5


def _render(stats: dict, final: bool = False) -> str:
    """渲染入口保留在本模块：PARTNER_TEMPLATE_LIMIT 是测试打桩面，必须现读"""
    return render(stats, final=final, partner_limit=PARTNER_TEMPLATE_LIMIT)


def build_daily_report(app, day: date | None = None, final: bool = False) -> str:
    """生成某天的日报（区间版的特例，保持原签名）"""
    day = day or date.today()
    return _render(_collect_stats(app, day, day), final=final)


def build_period_report(app, start: date, end: date) -> str:
    """生成区间汇总日报（本周/本月/上周）。start > end 时自动交换。"""
    if start > end:
        start, end = end, start
    return _render(_collect_stats(app, start, end))


def _verify_onebot_response(resp) -> tuple[bool, str]:
    """校验 OneBot v11 的**响应体**，HTTP 200 不代表推成功。

    为什么必须看 body：OneBot 的失败语义放在响应体里——
      HTTP 200 + {"status":"failed","retcode":100,"message":"GROUP_NOT_FOUND"}
    群号写错、机器人被踢出群、Token 轮换后没同步、被风控、消息超长，
    全都走这条路径。只看 status_code 的结果是：日志绿、日报徽标绿、仪表盘绿，
    群里一条消息都没有；而且"定时日报未推送成功"这条兜底告警的判断条件就是
    pushed 为假，于是它**永远不会触发**——整套告警是有洞的。
    """
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON（老版本 NapCat / 反代错误页）
        return True, ""
    if not isinstance(payload, dict):
        return True, ""

    status = str(payload.get("status") or "").strip().lower()
    retcode = payload.get("retcode")
    try:
        retcode_int = int(retcode) if retcode is not None else None
    except (TypeError, ValueError):
        retcode_int = -1  # 拿到看不懂的 retcode，按失败处理（宁可误报也别假成功）
    if status == "failed" or (retcode_int is not None and retcode_int not in (0, 1)):
        reason = (
            payload.get("message") or payload.get("wording") or payload.get("msg") or "未知原因"
        )
        return False, f"{reason}（status={status or '-'} retcode={retcode}）"
    return True, ""


def _post_with_retry(action: str, payload: dict, description: str) -> tuple[bool, str]:
    """带重试的 OneBot 动作调用。description 只用于日志，不带密钥。

    重试边界按"重发一次会不会有不同结果"划：
      - 连接异常 / 超时 / 5xx：对方可能只是抖一下，退避重试
      - 4xx：请求本身被拒（Token 错、群号写错、消息过长），重发同样失败，
        立刻返回——重试只会让人晚 20 秒才知道群里没消息
      - 200 但 body 说 failed：业务失败（群不存在/被踢/风控），同样不重试
    """
    from server.app.config import Config

    webhook = Config.NAPCAT_WEBHOOK.rstrip("/")
    if not webhook:
        logger.info("未配置 NAPCAT_WEBHOOK，跳过%s（日报已存档）", description)
        return False, "未配置 NAPCAT_WEBHOOK，已跳过推送"
    if action == "send_group_msg" and not str(payload.get("group_id") or "").strip():
        logger.warning("已配置 NAPCAT_WEBHOOK 但缺少群号（REPORT_GROUP_ID），跳过%s", description)
        return False, "缺少 REPORT_GROUP_ID，已跳过推送"

    import requests

    headers = {"Content-Type": "application/json"}
    if Config.NAPCAT_TOKEN:
        headers["Authorization"] = f"Bearer {Config.NAPCAT_TOKEN}"

    last = "未知错误"
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        try:
            resp = requests.post(f"{webhook}/{action}", json=payload, headers=headers, timeout=10)
        except requests.RequestException as e:
            last = f"推送异常: {e}"
            logger.error("第 %d/%d 次%s异常: %s", attempt, PUSH_ATTEMPTS, description, e)
            if attempt < PUSH_ATTEMPTS:
                wait = PUSH_BACKOFF_S[min(attempt - 1, len(PUSH_BACKOFF_S) - 1)]
                logger.warning("%ds 后重试%s", wait, description)
                time.sleep(wait)
            continue

        body = getattr(resp, "text", "") or ""
        if 400 <= resp.status_code < 500:
            logger.error("%s被拒绝 HTTP %s（不重试）: %s", description, resp.status_code, body[:200])
            return False, f"推送失败 HTTP {resp.status_code}（请求被拒绝，不重试）: {body[:120]}"

        if resp.status_code == 200:
            ok, detail = _verify_onebot_response(resp)
            if ok:
                logger.info("%s成功（%s）", description, action)
                return True, "推送成功"
            logger.error("%s被 OneBot 拒绝（HTTP 200 但 body 失败）: %s", description, detail)
            return False, f"推送被 OneBot 拒绝: {detail}"

        last = f"推送失败 HTTP {resp.status_code}"
        logger.error(
            "第 %d/%d 次%s失败: %s %s", attempt, PUSH_ATTEMPTS, description, resp.status_code, body[:200]
        )
        if attempt < PUSH_ATTEMPTS:
            wait = PUSH_BACKOFF_S[min(attempt - 1, len(PUSH_BACKOFF_S) - 1)]
            logger.warning("%ds 后重试%s", wait, description)
            time.sleep(wait)
    return False, f"{last}（已重试 {PUSH_ATTEMPTS} 次）"


def push_to_group(content: str, group_id=None) -> tuple[bool, str]:
    """推送到 QQ 群。未配置视为"跳过"而非失败；失败自动重试。

    group_id 为空时用 REPORT_GROUP_ID（定时日报）。指定 group_id 只用于
    「群内指令私聊失败后回原群」——绝不能借默认值把报表发到别的群去。
    """
    from server.app.config import Config

    target = str(group_id) if group_id is not None else Config.REPORT_GROUP_ID
    payload = {
        "group_id": int(target) if target.isdigit() else target,
        "message": [{"type": "text", "data": {"text": content}}],
    }
    return _post_with_retry("send_group_msg", payload, "日报推送")


def push_to_user(user_id: int | str, content: str) -> tuple[bool, str]:
    """私聊推送给单个 QQ 号（群内触发指令时回复触发者用）。

    与 push_to_group 共用重试逻辑；未配置 NAPCAT_WEBHOOK 时同样只跳过。
    """
    payload = {
        "user_id": int(user_id) if str(user_id).isdigit() else user_id,
        "message": [{"type": "text", "data": {"text": content}}],
    }
    return _post_with_retry("send_private_msg", payload, f"私聊推送 {user_id}")


def run_daily(
    app,
    day: date | None = None,
    push: bool = True,
    record_status: bool = True,
    force: bool = False,
    final: bool = False,
) -> dict:
    """生成 + 存档 + 推送 + 记录推送状态。返回结构化结果供接口/定时任务使用。

    record_status=False 时不覆盖 reports/<日期>.status.json——
    群内指令触发（不推群）不能把定时推送的「已推送」徽标刷成「未推送」。

    幂等闸门（force=False 时生效）：如果这一天已经推送成功，且**数据指纹没变**，
    就直接跳过推送。它挡掉三种真实场景：
      - 服务重启后 job 重跑（同一天两条一模一样的日报）
      - 多实例/重复部署同时触发
      - 次日终版巡检时，昨天其实一次都没补录
    手点「重推此日报」必须 force=True，否则按钮点了没反应。

    final=True 表示"次日终版"：18:00 之后补录的记录这才进得来。
    """
    day = day or date.today()
    stats = _collect_stats(app, day, day)
    content = _render(stats, final=final)
    path = save_report(app, content, day)
    fingerprint = stats_fingerprint(stats)
    prev = load_push_status(day)

    if not push:
        pushed, message, skipped = False, "未推送", False
    elif not force and prev.get("pushed") and prev.get("fingerprint") == fingerprint:
        pushed, message, skipped = True, "内容与已推送版本一致，已跳过重复推送", True
    else:
        pushed, message = push_to_group(content)
        skipped = False

    if record_status:
        status = dict(prev)
        status.update(
            {
                "date": day.isoformat(),
                "pushed": pushed,
                "message": message,
                "fingerprint": fingerprint,
            }
        )
        if pushed and not skipped:
            status["pushed_at"] = datetime.now().isoformat(timespec="seconds")
            status["attempts"] = int(prev.get("attempts") or 0) + 1
            status["last_error"] = ""
        elif not pushed:
            status["attempts"] = int(prev.get("attempts") or 0) + 1
            status["last_error"] = message
            # 从没成功过就是 None；成功过但这次失败，保留上次成功时间（它确实推出去过）
            status["pushed_at"] = prev.get("pushed_at")
        if final:
            status["final"] = True
            status["final_at"] = datetime.now().isoformat(timespec="seconds")
        save_push_status(day, status)

    return {
        "date": day.isoformat(),
        "path": str(path),
        "pushed": pushed,
        "message": message,
        "skipped": skipped,
        "final": final,
        "content": content,
    }


def yesterday() -> date:
    return date.today() - timedelta(days=1)


__all__ = [
    "OPS_ALERT_MIN_INTERVAL_S",
    "PARTNER_TEMPLATE_LIMIT",
    "PUSH_ATTEMPTS",
    "PUSH_BACKOFF_S",
    "build_daily_report",
    "build_period_report",
    "push_alert",
    "push_ops_alert",
    "push_to_group",
    "push_to_user",
    "reset_alert_throttle",
    "reset_ops_alert_throttle",
    "run_daily",
    "save_push_status",
    "save_report",
    "yesterday",
]
