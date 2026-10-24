"""登录限流（server/app/ratelimit.py）单元测试

安全控制：后台登录口的失败次数限制。已有的 test_web 只从登录接口侧
验了「超限 429 / 成功清零」，这里补的是限流器自身的窗口与内存回收语义。
每次用例都换一份全新的计数表，避免把限流状态漏给同进程里的其它用例。
"""

from __future__ import annotations

from collections import defaultdict, deque
from types import SimpleNamespace

import pytest

from server.app import ratelimit


@pytest.fixture()
def rl(monkeypatch) -> SimpleNamespace:
    """全新计数表 + 可控单调时钟（限流必须走单调时钟，NTP 回拨不能失效）"""
    monkeypatch.setattr(ratelimit, "_HITS", defaultdict(deque))
    clock = {"now": 1000.0}
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    ns = SimpleNamespace(clock=clock)
    return ns


def test_allows_until_limit_then_blocks(rl):
    assert ratelimit.hit("login:1.2.3.4", limit=3, window=60) == (True, 0)
    assert ratelimit.hit("login:1.2.3.4", limit=3, window=60) == (True, 0)
    assert ratelimit.hit("login:1.2.3.4", limit=3, window=60) == (True, 0)

    allowed, retry_after = ratelimit.hit("login:1.2.3.4", limit=3, window=60)
    assert allowed is False
    assert 1 <= retry_after <= 61  # 给调用方一个建议等待秒数，不是 0 也不是负数

    # 别的来源不受影响（按 key 隔离）
    assert ratelimit.hit("login:9.9.9.9", limit=3, window=60) == (True, 0)


def test_window_expiry_reopens_the_gate(rl):
    """窗口过后旧失败记录自动出队，否则一次误封就是永久封"""
    ratelimit.hit("login:1.2.3.4", limit=1, window=60)
    assert ratelimit.hit("login:1.2.3.4", limit=1, window=60)[0] is False

    rl.clock["now"] += 61

    assert ratelimit.hit("login:1.2.3.4", limit=1, window=60) == (True, 0)


def test_reset_clears_the_key(rl):
    ratelimit.hit("login:1.2.3.4", limit=1, window=60)
    assert ratelimit.hit("login:1.2.3.4", limit=1, window=60)[0] is False

    ratelimit.reset("login:1.2.3.4")  # 登录成功立刻清零

    assert dict(ratelimit._HITS) == {}  # 计数被清掉，还没人重新计数
    assert ratelimit.hit("login:1.2.3.4", limit=1, window=60) == (True, 0)


def test_reset_unknown_key_is_noop(rl):
    ratelimit.reset("login:从未出现过")
    assert dict(ratelimit._HITS) == {}


def test_sweep_reclaims_expired_keys_before_growth(rl):
    """键数超过阈值时顺手回收过期队列——否则长期运行攒出内存垃圾"""
    for i in range(ratelimit._SWEEP_AT + 1):
        ok, _ = ratelimit.hit(f"login:10.0.0.{i}", limit=5, window=60)
        assert ok
    assert len(ratelimit._HITS) > ratelimit._SWEEP_AT

    rl.clock["now"] += 3600  # 全部过窗口

    assert ratelimit.hit("login:new", limit=5, window=60) == (True, 0)
    # 过期键被删除，只留下本次真正活跃的键
    assert set(ratelimit._HITS) == {"login:new"}
