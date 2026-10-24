"""时间区间与展示参数解析：快捷 chip、自定义区间、伙伴明细条数。

只做"把请求参数变成合法值或抛 RangeError"，不碰数据库——
调用方拿 (start, end) 去 build_overview 聚合。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# 快捷区间的天数跨度（含今天）。昨天是特殊单日档。
PRESETS: dict[str, int] = {"today": 0, "yesterday": 1, "7d": 6, "30d": 29}
PRESET_LABELS = {
    "today": "今天",
    "yesterday": "昨天",
    "7d": "近 7 天",
    "30d": "近 30 天",
    "custom": "自定义",
}
MAX_CUSTOM_DAYS = 90  # 再长趋势图就糊成一条线，没有查看价值

# 伙伴明细默认只出前 20 家；0 = 全部（导出与「显示全部」按钮用）
DEFAULT_PARTNER_LIMIT = 20
MAX_PARTNER_LIMIT = 500


class RangeError(ValueError):
    """时间区间不合法：消息直接给用户看"""


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_range(args) -> tuple[date, date, str, str]:
    """把请求参数解析成 (start, end, label, preset)。

    preset 用于页面高亮当前 chip；custom 时前端回显输入的起止日期。
    """
    preset = (args.get("range") or "7d").strip()
    today = date.today()

    if preset == "custom":
        start = _parse_date(args.get("start"))
        end = _parse_date(args.get("end"))
        if start is None or end is None:
            raise RangeError("自定义区间需要完整的开始和结束日期（格式 YYYY-MM-DD）")
        if start > end:
            raise RangeError("开始日期不能晚于结束日期，请检查后重试")
        if end > today:
            raise RangeError("结束日期不能晚于今天")
        if (end - start).days + 1 > MAX_CUSTOM_DAYS:
            raise RangeError(f"自定义区间最长 {MAX_CUSTOM_DAYS} 天，趋势图超出后无法细看")
        return start, end, PRESET_LABELS["custom"], preset

    if preset not in PRESETS:
        raise RangeError("不支持的时间范围，请用页面上的快捷区间")

    if preset == "yesterday":
        day = today - timedelta(days=1)
        return day, day, PRESET_LABELS["yesterday"], preset
    return today - timedelta(days=PRESETS[preset]), today, PRESET_LABELS[preset], preset


def parse_limit(args) -> int:
    """伙伴明细的展示条数：默认 20，0 = 全部，上限 500。

    非法值不报错、退回默认：这是展示偏好，不是安全边界——
    为一个手打的 limit 参数返回 400，只会让人以为数据坏了。
    """
    raw = (args.get("limit") or "").strip()
    if raw == "":
        return DEFAULT_PARTNER_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PARTNER_LIMIT
    if value == 0:
        return 0
    return min(max(value, 1), MAX_PARTNER_LIMIT)
