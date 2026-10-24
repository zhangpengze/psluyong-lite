"""概览的 15 秒缓存：轮询 30 秒一次，多数轮询撞缓存；手动刷新之间也不会重复算。

记录写入（新增/删除/恢复）会主动 invalidate，主管看着页面也能近实时。
"""

from __future__ import annotations

import time
from datetime import date

from .build import build_overview

_CACHE: dict[tuple[date, date, str], tuple[float, dict]] = {}
_CACHE_TTL = 15.0
_CACHE_MAX = 32


def invalidate() -> None:
    """records 发生变化后调用：清掉概览缓存，下次请求现算"""
    _CACHE.clear()


def get_overview(start: date, end: date, label: str, preset: str) -> dict:
    """带 15 秒缓存的概览入口。返回的 dict 只读，调用方不要改。"""
    key = (start, end, preset)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    data = build_overview(start, end, label, preset)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))  # 键空间很小，粗暴淘汰最老一个
    _CACHE[key] = (now, data)
    return data
