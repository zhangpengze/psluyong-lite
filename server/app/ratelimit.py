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


# ---------- 上传闸门（录用记录带图提交） ----------
# 单条记录最大 64MB（MAX_CONTENT_LENGTH）。一个人写出死循环、或者同一台机器
# 上有个脚本在重放旧请求，几分钟就能把 NAS 的盘写满、把内存吃穿——而正常
# 员工是"出一张图提交一次"，几分钟一次都算快的，所以额度可以给得很宽。
UPLOAD_PER_SOURCE = 30
# 全站合计（跨来源）：10 个人各自 30 次/分钟就是 300 张图，再高就该拦了
UPLOAD_GLOBAL = 300
UPLOAD_WINDOW_S = 60

# 分桶键用 request.remote_addr（客户端 IP），不读 X-Forwarded-For：那是个
# 请求头，谁都能填，读它等于把限流交给攻击者开。
# ⚠️ 如果哪天真在 waitress 前面加一层 nginx，所有人的 remote_addr 都会变成
# 反代自己，于是全公司挤进同一个来源桶（30 次/分）——那时必须给应用装
# werkzeug 的 ProxyFix，否则这个闸门会误伤正常提交。


def upload_allowed(source: str) -> tuple[bool, int]:
    """两道闸门：单来源 + 全站合计。返回 (是否放行, 建议等待秒数)。

    单来源先判、放行后才记全站额度：某人被自己那道闸门拦住时不该顺带
    消耗别人的额度。
    """
    key = f"upload:{source or '-'}"
    allowed, retry_after = hit(key, UPLOAD_PER_SOURCE, UPLOAD_WINDOW_S)
    if not allowed:
        return False, retry_after
    return hit("upload:*", UPLOAD_GLOBAL, UPLOAD_WINDOW_S)
