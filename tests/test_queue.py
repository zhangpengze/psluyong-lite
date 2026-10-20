"""离线队列的补传策略与死信处理

核心约定：
- 网络断（status=0）不该消耗重试次数——员工的记录不是"坏数据"
- 4xx 是业务拒绝（比如重复），重试一万次也一样，直接丢
- 5xx 可能是毒记录（图片被删了），要有上限，否则永远堵住队头
- 连续多条 5xx 说明服务端整体挂了，别再一条条试
"""

from __future__ import annotations

import json

import pytest

from client.sidecar import queue
from client.sidecar.api_client import ServerError


@pytest.fixture()
def qdir(tmp_path, monkeypatch):
    """每个用例独立的队列目录，避免互相污染真实 data 目录"""
    pending = tmp_path / "pending"
    dead = tmp_path / "dead"
    monkeypatch.setattr(queue, "QUEUE_DIR", pending)
    monkeypatch.setattr(queue, "DEAD_DIR", dead)
    return pending, dead


class FakeClient:
    """按预设脚本返回结果的假服务端

    outcomes 里每项是 None（成功）或一个 status 码（抛 ServerError）。
    """

    def __init__(self, outcomes: list[int | None]):
        self.outcomes = list(outcomes)
        self.calls = 0

    def submit_record(self, fields, image_path=None):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is None:
            return {"id": self.calls}
        raise ServerError(f"错误 {outcome}", status=outcome)


def _enqueue(author: str = "张三") -> None:
    queue.enqueue({"author": author, "title": "论文"}, None)


def test_flush_success_removes_record(qdir):
    pending, _dead = qdir
    _enqueue()
    _enqueue("李四")

    result = queue.flush(FakeClient([None, None]))

    assert result["sent"] == 2
    assert result["remaining"] == 0
    assert list(pending.glob("*.json")) == []


def test_network_down_keeps_record_without_counting(qdir):
    """status=0 是网络不通，不能算失败次数，否则断网一天记录就全进死信了"""
    pending, dead = qdir
    _enqueue()

    result = queue.flush(FakeClient([0]))

    assert result["sent"] == 0
    assert result["remaining"] == 1
    files = list(pending.glob("*.json"))
    assert len(files) == 1
    item = json.loads(files[0].read_text(encoding="utf-8"))
    assert "failures" not in item, "网络故障不应计入重试次数"
    assert list(dead.glob("*.json")) == []


def test_network_down_breaks_immediately(qdir):
    """网络不通时后面的大概率也失败，不该挨个去撞超时"""
    _enqueue()
    _enqueue()
    _enqueue()
    client = FakeClient([0, 0, 0])

    queue.flush(client)

    assert client.calls == 1, "第一次就断网，不应该继续尝试"


def test_single_record_becomes_dead_letter_after_max_attempts(qdir):
    pending, dead = qdir
    _enqueue()
    # 每次 flush 只访问该记录一次，失败一次
    client = FakeClient([500] * queue.MAX_ATTEMPTS)

    for _ in range(queue.MAX_ATTEMPTS - 1):
        result = queue.flush(client)
        assert result["remaining"] == 1, "未达上限应留在队列里继续重试"
        assert list(dead.glob("*.json")) == []

    result = queue.flush(client)

    assert result["remaining"] == 0
    assert result["dead"] == 1
    dead_files = list(dead.glob("*.json"))
    assert len(dead_files) == 1
    item = json.loads(dead_files[0].read_text(encoding="utf-8"))
    assert item["failures"] == queue.MAX_ATTEMPTS
    assert "dead_reason" in item and "dead_at" in item
    assert item["fields"]["author"] == "张三", "死信要留完整内容，方便人工补录"
    assert list(pending.glob("*.json")) == []


def test_consecutive_5xx_stops_flush(qdir):
    """连续 3 条都 5xx，判断为服务端整体故障，剩下的先不动"""
    pending, _dead = qdir
    for name in ("甲", "乙", "丙", "丁"):
        _enqueue(name)
    client = FakeClient([500] * 4)

    queue.flush(client)

    assert client.calls == queue.CONSECUTIVE_5XX_LIMIT
    files = sorted(pending.glob("*.json"))
    assert len(files) == 4
    items = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    counted = [i for i in items if "failures" in i]
    assert len(counted) == queue.CONSECUTIVE_5XX_LIMIT, "只给试过的那几条记失败"
    untouched = [i for i in items if "failures" not in i]
    assert len(untouched) == 1, "没轮到的记录不该被记失败"


def test_success_resets_consecutive_counter(qdir):
    """500 → 成功 → 500，不该被判成连续故障而中断"""
    pending, _dead = qdir
    _enqueue("甲")
    _enqueue("乙")
    _enqueue("丙")
    _enqueue("丁")
    client = FakeClient([500, None, 500, None])

    result = queue.flush(client)

    assert client.calls == 4, "中间有成功就说明服务端还活着，应继续补传"
    assert result["sent"] == 2
    assert result["remaining"] == 2


def test_4xx_drops_record_without_retry(qdir):
    """业务拒绝（如重复录入）重试没有意义"""
    pending, dead = qdir
    _enqueue()

    result = queue.flush(FakeClient([409]))

    assert result["skipped"] == 1
    assert result["remaining"] == 0
    assert list(pending.glob("*.json")) == []
    assert list(dead.glob("*.json")) == [], "业务拒绝不算死信"


def test_corrupt_file_moved_to_dead_with_raw(qdir):
    pending, dead = qdir
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "20260101_000000_abcdef.json").write_text("{不是 JSON", encoding="utf-8")

    result = queue.flush(FakeClient([]))

    assert result["skipped"] == 1
    assert list(pending.glob("*.json")) == []
    dead_files = list(dead.glob("*.json"))
    assert len(dead_files) == 1
    item = json.loads(dead_files[0].read_text(encoding="utf-8"))
    assert "不是 JSON" in item["corrupt_raw"], "损坏文件要留原文，不能悄悄删掉"


def test_retry_dead_requeues_and_clears_failures(qdir):
    pending, dead = qdir
    dead.mkdir(parents=True, exist_ok=True)
    (dead / "20260101_000000_abcdef.json").write_text(
        json.dumps(
            {
                "fields": {"author": "张三", "title": "论文"},
                "image_path": None,
                "failures": queue.MAX_ATTEMPTS,
                "dead_reason": "连续 5 次服务端错误",
                "dead_at": "2026-01-01T00:00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = queue.retry_dead()

    assert result["restored"] == 1
    assert result["dead"] == 0
    files = list(pending.glob("*.json"))
    assert len(files) == 1
    item = json.loads(files[0].read_text(encoding="utf-8"))
    assert "failures" not in item, "清掉计数，否则下一次 5xx 立刻又被打回死信"
    assert "dead_reason" not in item and "dead_at" not in item


def test_retry_dead_leaves_corrupt_letter_alone(qdir):
    """只有 corrupt_raw 没有 fields 的死信搬回去也提交不了"""
    pending, dead = qdir
    dead.mkdir(parents=True, exist_ok=True)
    (dead / "broken.json").write_text(
        json.dumps({"corrupt_raw": "{不是 JSON", "dead_reason": "文件损坏"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = queue.retry_dead()

    assert result["restored"] == 0
    assert result["failed"] == 1
    assert result["dead"] == 1
    assert list(pending.glob("*.json")) == []


def test_retry_dead_then_flush_succeeds(qdir):
    """恢复后的完整链路：死信回队列 → 服务端恢复 → 补传成功"""
    pending, dead = qdir
    dead.mkdir(parents=True, exist_ok=True)
    (dead / "20260101_000000_abcdef.json").write_text(
        json.dumps({"fields": {"author": "张三"}, "failures": 5}, ensure_ascii=False),
        encoding="utf-8",
    )

    queue.retry_dead()
    result = queue.flush(FakeClient([None]))

    assert result["sent"] == 1
    assert result["remaining"] == 0
    assert list(dead.glob("*.json")) == []
