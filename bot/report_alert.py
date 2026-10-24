"""告警推送与节流（拆自 report.py）。

两类告警各自独立节流：
  - push_alert：员工录用失败即时告警，间隔取 Config.ALERT_MIN_INTERVAL_S，
    一个全局哨兵（PS 挂了员工连点 5 次，是同一件事，压成一条）
  - push_ops_alert：运维告警，**按 (kind, title) 分开计时**，5 分钟窗口

为什么运维告警不能用一个全局哨兵：`nightly_job` 一次跑备份 → 镜像 → 磁盘三步，
三步都失败时是三条**互不相干**的告警。用全局哨兵时只有第一条能出去，后两条
连日志都没有——凌晨最该知道的"图没镜像"就这么没了。同理，日报未推送与备份失败
撞在同一分钟也不该互相吞。

HTTP 调用复用 report._post_with_retry（重试常量以 report 模块为准，
测试 monkeypatch report.PUSH_BACKOFF_S 对这里同样生效），故函数内延迟 import。
"""

from __future__ import annotations

import json
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


# ========== 运维告警 ==========

_ops_lock = threading.Lock()
# key = (kind, title) → 上次真正发出去的时刻。按主题分开计时，互不挤占。
_ops_last_sent: dict[tuple[str, str], float] = {}
# 被压掉了多少次，恢复发送时一并告知——"悄悄少了两条"比"多发一条"难查得多
_ops_suppressed: dict[tuple[str, str], int] = {}
OPS_ALERT_MIN_INTERVAL_S = 300


def reset_ops_alert_throttle() -> None:
    """测试用：清空运维告警节流"""
    with _ops_lock:
        _ops_last_sent.clear()
        _ops_suppressed.clear()


def record_local_alert(kind: str, title: str, detail: str, state: str) -> None:
    """本地告警流水：`<DATA_DIR>/logs/ops-alerts.jsonl`。

    为什么群消息之外还要落一份：告警通道就是被监控的 NapCat 本身。
    NapCat 挂掉时"日报推不出去"这件事永远发不出群消息——最该知道的故障
    恰好静默。落一份本地文件，运维打开目录就能看到这几天到底报了些什么。
    """
    try:
        from server.app.config import DATA_DIR

        path = DATA_DIR / "logs" / "ops-alerts.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind,
                "title": title,
                "detail": detail[:500],
                "state": state,
            },
            ensure_ascii=False,
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except (OSError, ValueError) as e:  # 记不下来也不能反过来把调用方搞崩
        logger.warning("本地告警流水写入失败: %s", e)


def push_ops_alert(title: str, detail: str = "", kind: str = "ops") -> tuple[bool, str]:
    """系统级问题告警。

    这些东西以前只写进日志：容器日志一轮转就消失，而"备份连续失败三天"
    "日报根本没推出去"恰恰是没人会主动去看的那类问题。宁可推一条群消息。

    kind 用来分组：同一个 kind + 同一句话才算重复。默认 "ops"，
    调用方按领域传（backup / mirror / disk / report / patrol）。
    """
    from server.app.config import Config

    from .report import _post_with_retry

    key = (kind, title)
    suppressed = 0

    if not Config.ALERT_ON_OPS_FAILURE:
        record_local_alert(kind, title, detail, "disabled")
        return False, "运维告警已关闭（ALERT_ON_OPS_FAILURE=0）"
    if not Config.NAPCAT_WEBHOOK or not Config.REPORT_GROUP_ID:
        # 没接机器人时只记日志，不报错——本地开发不该因为这条炸掉
        logger.warning("运维告警未发送（未配置推送）：%s｜%s", title, detail)
        record_local_alert(kind, title, detail, "unconfigured")
        return False, "未配置推送通道"

    now = time.monotonic()
    with _ops_lock:
        last = _ops_last_sent.get(key)
        if last is not None and now - last < OPS_ALERT_MIN_INTERVAL_S:
            _ops_suppressed[key] = _ops_suppressed.get(key, 0) + 1
            suppressed = _ops_suppressed[key]
        else:
            _ops_last_sent[key] = now
            _ops_suppressed.pop(key, None)

    if suppressed:
        # 以前这里直接 return，连日志都没有：运维事后只能看到"少了两条"，
        # 无从判断是被节流了还是根本没触发
        logger.warning(
            "运维告警已抑制（%ds 窗口内同题第 %d 次）：[%s] %s｜%s",
            OPS_ALERT_MIN_INTERVAL_S,
            suppressed,
            kind,
            title,
            detail,
        )
        record_local_alert(kind, title, detail, f"suppressed#{suppressed}")
        return False, f"同类告警 {OPS_ALERT_MIN_INTERVAL_S}s 内已发过，本次已抑制（第 {suppressed} 次）"

    content = f"【系统告警】{title}\n{detail}\n时间：{datetime.now():%m-%d %H:%M}"
    group_id = Config.REPORT_GROUP_ID
    payload = {
        "group_id": int(group_id) if group_id.isdigit() else group_id,
        "message": [{"type": "text", "data": {"text": content}}],
    }
    ok, message = _post_with_retry("send_group_msg", payload, "运维告警")
    record_local_alert(kind, title, detail, "sent" if ok else f"failed:{message}"[:120])
    return ok, message
