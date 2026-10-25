"""
日报生成与推送

设计取舍：
  - 报告先落盘成 Markdown（data/reports/YYYY-MM-DD.md），再尝试推送。
    推送失败不影响存档，后台"每日日报"页仍能看到当天数据。
  - 推送走官方 QQ 机器人（see qqbot_sender.py）。没配机器人时只落盘 + 记日志，
    不报错——本地开发和没接机器人的部署不该因此炸掉。
  - 单日与区间（本周/本月）共用一套「_collect_stats + _render」：
    区间版只多一个「按日」段落，统计口径完全一致。

文件拆分（单文件 ≤300 行）：区间统计在 report_stats.py，落盘在 report_store.py，
告警与节流在 report_alert.py，HTTP 与鉴权在 qqbot_sender.py；
本文件保留渲染与 run_daily 编排——测试以 bot.report 为 monkeypatch 面
（push_to_group / PARTNER_TEMPLATE_LIMIT），这些名字必须留在本模块。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from .qqbot_sender import push_to_group
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
    后台「只生成不推送」不能把定时推送的「已推送」徽标刷成「未推送」。

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
    "build_daily_report",
    "build_period_report",
    "push_alert",
    "push_ops_alert",
    # 推送实现在 qqbot_sender，这里只做转发——名字留在 report 是因为
    # run_daily 与接口层都以 bot.report 为门面（也方便测试直接打桩）。
    "push_to_group",
    "reset_alert_throttle",
    "reset_ops_alert_throttle",
    "run_daily",
    "save_push_status",
    "save_report",
    "yesterday",
]
