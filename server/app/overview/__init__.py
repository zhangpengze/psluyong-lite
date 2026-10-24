"""仪表盘概览：KPI / 趋势 / 合作伙伴明细（含各模板用量）。

口径（全站只有这一处定义，页面底部也会原样展示）：
    调用次数 = is_deleted=0 的全部记录，含失败；成功率单独算，不掺进次数。
    业务日 = work_date（员工填的工作日期），不是 created_at——员工晚上补传
    昨天的记录时，日报按工作日期归属，不能因为传晚了就归到第二天。
    合作伙伴只统计 partner_name 非空的记录；空伙伴名的记录照常计入总量。

按子功能拆分（单文件 ≤300 行门禁）：
    range.py   时间区间与展示参数解析（RangeError / parse_range / parse_limit）
    build.py   数据聚合（build_overview，口径常量也在这里）
    cache.py   15 秒缓存与 invalidate（写记录后清缓存）
本文件是兼容门面：原 `from server.app.overview import ...` 的 import 方不用改，
`_CACHE` 等名字 re-export 的是 cache.py 里的同一个对象。
"""

from __future__ import annotations

from .build import (
    ACTIVE_MAX_DAYS,
    CALLS_DEFINITION,
    STATUS_LABELS,
    WARNING_MAX_DAYS,
    build_overview,
)
from .cache import _CACHE, get_overview, invalidate
from .range import (
    DEFAULT_PARTNER_LIMIT,
    MAX_CUSTOM_DAYS,
    MAX_PARTNER_LIMIT,
    PRESETS,
    PRESET_LABELS,
    RangeError,
    parse_limit,
    parse_range,
)

__all__ = [
    "ACTIVE_MAX_DAYS",
    "CALLS_DEFINITION",
    "DEFAULT_PARTNER_LIMIT",
    "MAX_CUSTOM_DAYS",
    "MAX_PARTNER_LIMIT",
    "PRESETS",
    "PRESET_LABELS",
    "RangeError",
    "STATUS_LABELS",
    "WARNING_MAX_DAYS",
    "_CACHE",
    "build_overview",
    "get_overview",
    "invalidate",
    "parse_limit",
    "parse_range",
]
