"""极简内存限流

只解决一个具体问题：后台登录口没有失败次数限制，谁都能拿字典慢慢试
管理员的密码。内网也不该裸奔——NAS 上这个端口往往整层楼可达。

取舍：
  - 内存计数，不引 Redis。单进程 waitress 下完全够用；将来多副本再换。
  - 只对"失败"计数，登录成功立刻清零，正常用户无感。
  - 时间窗口用单调时钟，避免系统时间被 NTP 回拨后限流失效。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_LOCK = threading.Lock()
_HITS: dict[str, deque[float]] = defaultdict(deque)

# 清理阈值：键数量超过这个值时顺手回收空队列，避免长期运行攒出内存垃圾
_SWEEP_AT = 512


def _sweep(now: float, window: float) -> None:
    for key in list(_HITS):
        queue = _HITS[key]
        while queue and now - queue[0] > window:
            queue.popleft()
        if not queue:
            del _HITS[key]


def hit(key: str, limit: int, window: float) -> tuple[bool, int]:
    """记一次尝试。

    返回 (allowed, retry_after)：
      - allowed=True 表示这次还在额度内，调用方继续走业务逻辑
      - allowed=False 表示已超限，retry_after 是建议的等待秒数
    """
    now = time.monotonic()
    with _LOCK:
        if len(_HITS) > _SWEEP_AT:
            _sweep(now, window)

        queue = _HITS[key]
        while queue and now - queue[0] > window:
            queue.popleft()

        if len(queue) >= limit:
            retry_after = int(window - (now - queue[0])) + 1
            return False, max(1, retry_after)

        queue.append(now)
        return True, 0


def reset(key: str) -> None:
    """登录成功后清空该来源的失败记录"""
    with _LOCK:
        _HITS.pop(key, None)
