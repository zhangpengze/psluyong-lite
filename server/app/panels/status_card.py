"""仪表盘的系统状态卡（组件行式，参考云厂商状态页）。

行的数据与分档在 server/app/status.py；这里只负责标记。
"""

from __future__ import annotations

from html import escape

_STATUS_WORD = {"ok": "正常", "warn": "警告", "bad": "异常", "off": "未配置"}


def status_html(s: dict) -> str:
    """一张卡五行组件：状态点 + 组件名 · 状态 + 右侧真实指标。"""
    from ..status import SYS_COMPONENTS

    rows = []
    for key, label in SYS_COMPONENTS:
        row = s["rows"].get(key) or {"status": "off", "detail": "-"}
        tone = row["status"] if row["status"] in _STATUS_WORD else "off"
        rows.append(
            '<div class="sys-row">'
            f'<span class="sys-dot {tone}" aria-hidden="true"></span>'
            f'<span class="sys-name">{label}</span>'
            f'<span class="sys-status {tone}">· {_STATUS_WORD[tone]}</span>'
            f'<span class="sys-metric">{escape(row.get("detail") or "-")}</span>'
            "</div>"
        )
    return (
        '<div class="card"><h2>系统状态<a class="card-link" href="/audit">操作日志</a></h2>'
        '<div class="sys-rows">' + "".join(rows) + "</div></div>"
    )
