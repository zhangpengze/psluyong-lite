"""机器人访问控制：QQ 白名单、群白名单、触发冷却。

从 handler.py 拆出来（单文件 ≤300 行），也因为这三件事是"谁能用"的策略，
和"解析指令/生成回复"是两码事——改策略时不必读渲染逻辑。

2026-10-24 的两次收紧都在这层：
  1. QQ 白名单留空 = **谁都不能用**（旧：不限制）。机器人只要被拉进一个
     外部群，群里任何人都能把含员工姓名/稿件的报表私聊带走。
  2. 群白名单必须校验（旧：完全不看 group_id），留空则只允许日报群。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# 触发冷却：同一 user_id 在窗口内重复触发直接忽略（防连点/重复上报）
_cooldown: dict[int, float] = {}
_cooldown_lock = threading.Lock()


def parse_id_set(raw: str) -> set[int]:
    """'10001, 10002' → {10001, 10002}；脏值丢掉（全角逗号也认）"""
    out: set[int] = set()
    for part in (raw or "").replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
        elif part:
            logger.warning("QQ 号/群号配置里有非数字条目，已忽略: %r", part)
    return out


def allowed_user(user_id: int, whitelist: set[int]) -> bool:
    """白名单为空 = 谁都不能用（fail-closed）。要放宽就显式列人。"""
    return user_id in whitelist


def allowed_group(group_id, raw_allowed: str, report_group_id: str) -> bool:
    """只允许白名单里的群；留空则只允许日报群。

    报表正文含员工姓名与稿件标题，等于把内部数据开放给"机器人所在的每个群"。
    """
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return False
    allowed = parse_id_set(raw_allowed)
    if not allowed:
        allowed = parse_id_set(report_group_id)
    return gid in allowed


def cooldown_ok(user_id: int, window_s: int) -> bool:
    """同一个人窗口内只放行一次，并顺手打扫过期记录"""
    now = time.monotonic()
    with _cooldown_lock:
        last = _cooldown.get(user_id)
        if last is not None and now - last < window_s:
            return False
        _cooldown[user_id] = now
        if len(_cooldown) > 64:  # 攒够了再打扫，不用每次加锁都扫全表
            stale_before = now - window_s * 10
            for uid, ts in list(_cooldown.items()):
                if ts < stale_before:
                    del _cooldown[uid]
        return True


def reset_cooldown() -> None:
    """测试用：清空冷却记录。"""
    with _cooldown_lock:
        _cooldown.clear()
