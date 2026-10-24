"""面板片段的共用格式化小函数：无业务、无 IO（utils 纯函数）。"""

from __future__ import annotations


def _num(value) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)
