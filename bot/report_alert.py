"""告警推送与节流（拆自 report.py）。

两类告警各自独立节流：
  - push_alert：员工录用失败即时告警，间隔取 Config.ALERT_MIN_INTERVAL_S
  - push_ops_alert：运维告警（备份失败/镜像失败/磁盘超阈值/日报未推送），
    固定 5 分钟窗口。分开的原因：凌晨备份失败不能被白天的录用失败告警挤掉。

HTTP 调用复用 report._post_with_retry（重试常量以 report 模块为准，
测试 monkeypatch report.PUSH_BACKOFF_S 对这里同样生效），故函数内延迟 import。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)


# 失败告警的最小间隔门控：PS 挂了员工连点 5 次就是 5 条失败记录，
# 不设限会把群刷爆。值取 Config.ALERT_MIN_INTERVAL_S，测试里可调。
#
# 哨兵必须是 -inf，不能是 0.0：time.monotonic() 的起点是开机（容器里是容器启动），
# 不是 Unix 纪元。用 0.0 时，机器/容器启动不足一个窗口（如 300s）内
# `now - 0.0 < interval` 恒成立，首条告警会被当成"间隔内的重复"静默丢弃——
# 而机器刚重启恰恰是最需要告警的时刻。
_ALERT_NEVER = float("-inf")

_alert_lock = threading.Lock()
_last_alert_at: float = _ALERT_NEVER


def reset_alert_throttle() -> None:
    """测试用：清空告警门控"""
    global _last_alert_at
    with _alert_lock:
        _last_alert_at = _ALERT_NEVER


def push_alert(content: str, min_interval_s: int | None = None) -> tuple[bool, str]:
    """录用失败即时告警：推到 REPORT_GROUP_ID。

    未配置 NapCat 时静默跳过（本地开发/未接机器人时不该报错）；
    间隔内的重复告警被抑制——失败记录本身已落库，告警只是通知。
    """
    from server.app.config import Config

    from .report import _post_with_retry

    global _last_alert_at
    interval = Config.ALERT_MIN_INTERVAL_S if min_interval_s is None else min_interval_s

    if not Config.NAPCAT_WEBHOOK:
        return False, "未配置 NAPCAT_WEBHOOK，跳过告警"
    if not Config.REPORT_GROUP_ID:
        return False, "缺少 REPORT_GROUP_ID，跳过告警"

    now = time.monotonic()
    with _alert_lock:
        if now - _last_alert_at < interval:
            return False, f"告警间隔小于 {interval}s，已抑制"
        _last_alert_at = now

    group_id = Config.REPORT_GROUP_ID
    payload = {
        "group_id": int(group_id) if group_id.isdigit() else group_id,
        "message": [{"type": "text", "data": {"text": content}}],
    }
    return _post_with_retry("send_group_msg", payload, "失败告警")


# 运维告警（备份失败 / 镜像失败 / 磁盘超阈值 / 日报未推送）与"员工录用失败"
# 是两类信息，用**各自独立**的节流器：否则凌晨备份失败会被白天的录用失败
# 告警挤掉，反之亦然。运维类本来就不频繁，5 分钟窗口足够去重。
_ops_lock = threading.Lock()
_ops_last_alert_at: float = float("-inf")  # 同 _ALERT_NEVER：0.0 会在开机窗口内误抑制
OPS_ALERT_MIN_INTERVAL_S = 300


def reset_ops_alert_throttle() -> None:
    """测试用：清空运维告警节流"""
    global _ops_last_alert_at
    with _ops_lock:
        _ops_last_alert_at = float("-inf")


def push_ops_alert(title: str, detail: str = "") -> tuple[bool, str]:
    """系统级问题告警。

    这些东西以前只写进日志：容器日志一轮转就消失，而"备份连续失败三天"
    "日报根本没推出去"恰恰是没人会主动去看的那类问题。宁可推一条群消息。
    """
    from server.app.config import Config

    from .report import _post_with_retry

    global _ops_last_alert_at

    if not Config.ALERT_ON_OPS_FAILURE:
        return False, "运维告警已关闭（ALERT_ON_OPS_FAILURE=0）"
    if not Config.NAPCAT_WEBHOOK or not Config.REPORT_GROUP_ID:
        # 没接机器人时只记日志，不报错——本地开发不该因为这条炸掉
        logger.warning("运维告警未发送（未配置推送）：%s｜%s", title, detail)
        return False, "未配置推送通道"

    now = time.monotonic()
    with _ops_lock:
        if now - _ops_last_alert_at < OPS_ALERT_MIN_INTERVAL_S:
            return False, "运维告警间隔内，已抑制"
        _ops_last_alert_at = now

    content = f"【系统告警】{title}\n{detail}\n时间：{datetime.now():%m-%d %H:%M}"
    group_id = Config.REPORT_GROUP_ID
    payload = {
        "group_id": int(group_id) if group_id.isdigit() else group_id,
        "message": [{"type": "text", "data": {"text": content}}],
    }
    return _post_with_retry("send_group_msg", payload, "运维告警")
