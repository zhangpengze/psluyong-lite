"""离线队列的补传策略与死信处理

核心约定：
- 网络断（status=0）不该消耗重试次数——员工的记录不是"坏数据"
- 400/409/422 是"这条数据本身有问题"，重试一万次也一样，直接丢
- 401/403/404 是环境/配置问题（Key 轮换没同步、地址指向旧版本），
  **记录必须保留**——2026-10-24 修的就是这里：旧版一律 remove()，
  换 Key 的瞬间整批积压静默消失，界面还显示成补传成功
- 5xx 可能是毒记录（图片被删了），要有上限，否则永远堵住队头
- 连续多条 5xx 说明服务端整体挂了，别再一条条试
- 入队时成品图复制进缓冲（image_buffer）：员工清理输出目录后补传仍然带图；
  缓冲和原目录都找不到时**不提交**、转死信——不能落一条无图记录
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from client.sidecar import image_buffer, queue, queue_flush
from client.sidecar.api_client import ServerError


@pytest.fixture()
def qdir(tmp_path, monkeypatch):
    """每个用例独立的队列/死信/图缓冲目录，避免互相污染真实 data 目录"""
    pending = tmp_path / "pending"
    dead = tmp_path / "dead"
    monkeypatch.setattr(queue, "QUEUE_DIR", pending)
    monkeypatch.setattr(queue, "DEAD_DIR", dead)
    monkeypatch.setattr(image_buffer, "BUFFER_DIR", tmp_path / "pending_images")
    return pending, dead


def _buffer_files() -> list[Path]:
    return sorted(image_buffer.BUFFER_DIR.glob("*")) if image_buffer.BUFFER_DIR.exists() else []


def _fake_image(tmp_path, name: str = "张三_论文.jpg") -> Path:
    path = tmp_path / "out" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return path


class FakeClient:
    """按预设脚本返回结果的假服务端

    outcomes 里每项是 None（成功）或一个 status 码（抛 ServerError）。
    paths/existing 记录每次调用实际拿到的图片路径、以及调用当时文件是否存在——
    "补传时到底有没有真把图发上去"必须能在用例里断言。
    """

    def __init__(self, outcomes: list[int | None]):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.paths: list[Path | None] = []
        self.existing: list[bool] = []

    def submit_record(self, fields, image_path=None):
        self.calls += 1
        self.paths.append(image_path)
        self.existing.append(bool(image_path and image_path.is_file()))
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


@pytest.mark.parametrize("status", [400, 409, 422])
def test_discard_status_drops_record(qdir, status):
    """400/409/422 = 这条数据本身不合法，删掉而不是留成补不成功的毒记录"""
    pending, _dead = qdir
    _enqueue()

    result = queue.flush(FakeClient([status]))

    assert result["skipped"] == 1
    assert result["remaining"] == 0
    assert list(pending.glob("*.json")) == []


@pytest.mark.parametrize("status", [401, 403, 404])
def test_auth_or_address_error_keeps_records(qdir, status):
    """鉴权/地址错：记录是好的，一条都不能丢

    真实场景：轮换 API Key 后某台机器没同步、或客户端地址指向没有该接口的旧版本。
    旧版把 401/403/404 当"业务拒绝"逐条 remove()，整批积压静默消失。
    """
    pending, dead = qdir
    _enqueue("甲")
    _enqueue("乙")
    _enqueue("丙")

    result = queue.flush(FakeClient([status] * 3))

    assert result["skipped"] == 0, "环境问题不能算跳过（跳过=删除）"
    assert result["remaining"] == 3, "三条都要留在队列里"
    assert result["blocked"] == 1, "本轮只碰到队头一条就停了"
    assert list(dead.glob("*.json")) == [], "这不算死信，配置修好还能补"
    files = sorted(pending.glob("*.json"))
    blocked = [json.loads(p.read_text(encoding="utf-8")) for p in files if json.loads(p.read_text(encoding="utf-8")).get("blocked")]
    assert len(blocked) == 1
    assert str(status) in blocked[0]["blocked_reason"]


def test_blocked_record_uploads_after_config_fixed(qdir):
    """受阻的记录不是弃子：配置修好后下一次 flush 就能补上"""
    pending, _dead = qdir
    _enqueue()

    blocked = queue.flush(FakeClient([401]))
    assert blocked["remaining"] == 1

    fixed = queue.flush(FakeClient([None]))

    assert fixed["sent"] == 1
    assert fixed["remaining"] == 0
    assert fixed["blocked"] == 0, "成功后 blocked 标记随文件一起消失"
    assert list(pending.glob("*.json")) == []


def test_429_keeps_record_without_counting_failure(qdir):
    """限流是暂时的：保留记录、停本轮，但不能记成 5xx 失败次数"""
    pending, _dead = qdir
    _enqueue()

    result = queue.flush(FakeClient([429]))

    assert result["remaining"] == 1
    assert result["skipped"] == 0
    files = list(pending.glob("*.json"))
    item = json.loads(files[0].read_text(encoding="utf-8"))
    assert "failures" not in item, "限流不是这条记录的错，不该累计到死信"


def test_blocked_items_exposes_reason(qdir):
    """界面要靠这个提示员工去检查设置，而不是干看着数字不减少"""
    _enqueue()
    queue.flush(FakeClient([403]))

    items = queue.blocked_items()

    assert len(items) == 1
    assert "403" in items[0]["reason"]
    assert items[0]["name"].endswith(".json")


def test_enqueue_with_blocked_reason_marks_item(qdir):
    """新记录上报就撞 401 时直接带标记入队，界面能立刻给出原因"""
    pending, _dead = qdir

    path = queue.enqueue({"author": "张三"}, None, blocked_reason="HTTP 401: API Key 无效")

    item = json.loads(path.read_text(encoding="utf-8"))
    assert item["blocked"] is True
    assert "401" in item["blocked_reason"]


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


# ========== 成品图缓冲：补传时不能丢图 ==========


def test_enqueue_copies_image_into_buffer(qdir, tmp_path):
    """入队就把图复制走：员工把输出目录清空也不影响后面补传"""
    source = _fake_image(tmp_path)

    path = queue.enqueue({"author": "张三"}, str(source))

    item = json.loads(path.read_text(encoding="utf-8"))
    assert item["image_path"] == str(source), "原路径照留，方便排查这张图从哪来"
    buffered = Path(item["buffered_image"])
    assert buffered.is_file(), "副本必须真的落盘"
    assert buffered.parent == image_buffer.BUFFER_DIR
    assert buffered.stem == path.stem, "副本与队列 json 同名，保住对应关系"
    assert buffered.read_bytes() == source.read_bytes()


def test_flush_survives_output_dir_cleanup(qdir, tmp_path):
    """这是整个缓冲设计的理由：员工整理输出目录后，补传仍然带着图"""
    source = _fake_image(tmp_path)
    queue.enqueue({"author": "张三"}, str(source))
    source.unlink()  # 员工把输出目录清空了

    client = FakeClient([None])
    result = queue.flush(client)

    assert result["sent"] == 1
    assert client.existing == [True], "补传时必须真的带上图，不能因为文件不在就不带"


def test_missing_image_goes_to_dead_instead_of_image_less_record(qdir, tmp_path):
    """缓冲和原目录都没了：宁可在死信里留档，也不能建一条无图记录

    旧版行为：api_client 发现文件不在就不带文件提交，服务端落一条没图的记录，
    界面还显示"已同步"——员工永远不知道这张图丢了。
    """
    source = _fake_image(tmp_path)
    path = queue.enqueue({"author": "张三"}, str(source))
    item = json.loads(path.read_text(encoding="utf-8"))
    Path(item["buffered_image"]).unlink()
    source.unlink()

    client = FakeClient([None])
    result = queue.flush(client)

    assert client.calls == 0, "图都没了就不该再往服务端提交"
    assert result["lost"] == 1
    assert result["dead"] == 1
    assert result["sent"] == 0
    dead_items = [json.loads(p.read_text(encoding="utf-8")) for p in (qdir[1]).glob("*.json")]
    assert len(dead_items) == 1
    assert "成品图" in dead_items[0]["dead_reason"]
    assert dead_items[0]["fields"]["author"] == "张三", "字段要留着，人工还能补录"


def test_buffer_copy_failure_falls_back_to_original_path(qdir, tmp_path, monkeypatch):
    """磁盘满/杀软锁文件导致复制失败：记录照常入队，退回用原路径补传"""

    def boom(*_args, **_kwargs):
        raise OSError("磁盘已满")

    monkeypatch.setattr(image_buffer.shutil, "copy2", boom)
    source = _fake_image(tmp_path)

    path = queue.enqueue({"author": "张三"}, str(source))

    item = json.loads(path.read_text(encoding="utf-8"))
    assert "buffered_image" not in item, "复制失败就不该记一个不存在的副本路径"
    client = FakeClient([None])
    assert queue.flush(client)["sent"] == 1
    assert client.paths == [source]
    assert client.existing == [True]


def test_buffer_cleaned_after_successful_send(qdir, tmp_path):
    """副本不能越积越多：记录落地后缓冲里对应的图要一起删掉"""
    queue.enqueue({"author": "张三"}, str(_fake_image(tmp_path)))

    assert len(_buffer_files()) == 1
    queue.flush(FakeClient([None]))

    assert _buffer_files() == []


def test_buffer_cleaned_after_business_reject(qdir, tmp_path):
    """400/409/422 是这条数据本身不行：记录删了，副本也不该留着"""
    queue.enqueue({"author": "张三"}, str(_fake_image(tmp_path)))

    queue.flush(FakeClient([422]))

    assert _buffer_files() == []


def test_record_without_image_is_not_treated_as_lost(qdir):
    """本来就无图的记录（历史数据/失败上报）不能被判成"缺图"转死信"""
    _enqueue()

    client = FakeClient([None])
    result = queue.flush(client)

    assert result["lost"] == 0
    assert result["sent"] == 1
    assert client.paths == [None]


def test_dead_letter_keeps_image_and_retry_brings_it_back(qdir, tmp_path):
    """死信要自带图：留档却把图丢了，人工点"重试"还是补不上去"""
    pending, dead = qdir
    source = _fake_image(tmp_path)
    queue.enqueue({"author": "张三"}, str(source))
    source.unlink()  # 只有缓冲里有图了

    for _ in range(queue.MAX_ATTEMPTS):
        queue.flush(FakeClient([500]))

    dead_json = list(dead.glob("*.json"))
    dead_images = list(dead.glob("*.jpg"))
    assert len(dead_json) == 1 and len(dead_images) == 1, "死信目录要图 + json 成对"
    item = json.loads(dead_json[0].read_text(encoding="utf-8"))
    assert Path(item["buffered_image"]).parent == dead
    assert Path(item["buffered_image"]).is_file()
    assert _buffer_files() == [], "图挪进 dead/ 了，缓冲里不该再留一份"

    moved = queue.retry_dead()

    assert moved["restored"] == 1
    assert list(dead.glob("*.jpg")) == []
    assert len(_buffer_files()) == 1, "图要跟着记录搬回缓冲"
    client = FakeClient([None])
    assert queue.flush(client)["sent"] == 1
    assert client.existing == [True]
    assert _buffer_files() == []
    assert list(pending.glob("*.json")) == []


def test_prune_removes_expired_orphan_only(qdir, tmp_path):
    """只清"过期且没有对应记录"的副本——活记录的图一张都不能动"""
    pending, dead = qdir
    buf = image_buffer.BUFFER_DIR
    buf.mkdir(parents=True, exist_ok=True)
    pending.mkdir(parents=True, exist_ok=True)

    live = buf / "live.jpg"
    live.write_bytes(b"x")
    (pending / "live.json").write_text("{}", encoding="utf-8")

    orphan_old = buf / "orphan_old.jpg"
    orphan_old.write_bytes(b"x")
    old = time.time() - (image_buffer.RETENTION_DAYS + 1) * 86400
    os.utime(orphan_old, (old, old))

    orphan_fresh = buf / "orphan_fresh.jpg"
    orphan_fresh.write_bytes(b"x")

    removed = image_buffer.prune(pending, dead)

    assert removed == 1
    assert live.is_file(), "还在队列里的记录，副本必须留着"
    assert orphan_fresh.is_file(), "没过保留期的先不动（可能是刚入队还没写 json）"
    assert not orphan_old.exists()


def test_locate_prefers_buffer_over_original(qdir, tmp_path):
    """两份都在时优先用缓冲副本：原目录那张可能马上就被员工删了"""
    source = _fake_image(tmp_path)
    path = queue.enqueue({"author": "张三"}, str(source))
    item = json.loads(path.read_text(encoding="utf-8"))

    found, lost = image_buffer.locate(item)

    assert lost is False
    assert found == Path(item["buffered_image"])
    assert found != source


# ========== 并发：同一时刻只许一轮补传 ==========


def test_flush_refuses_when_another_flush_is_running(qdir):
    """两个窗口同时触发补传：第二个直接返回当前积压，不许再提交一遍

    否则同一批 5xx 记录会被两边各 +1，MAX_ATTEMPTS 提前用完——本该还能重试
    的记录被提前丢进死信，这是真丢数据；两个请求还会同时改写同一个 json。
    """
    _enqueue()
    client = FakeClient([None])

    with queue_flush._FLUSH_LOCK:
        refused = queue.flush(client)

    assert client.calls == 0, "已经有一轮在跑，不许再提交"
    assert refused["sent"] == 0
    assert refused["remaining"] == 1, "要如实告诉调用方还剩多少，不能显示成已清空"
    # 锁没被吃掉：那一轮结束后下一次照常能补
    assert queue.flush(client)["sent"] == 1


def test_empty_result_shape_matches_flush(qdir):
    """empty_result 是给"本轮没补传"用的，形状必须和 flush 一致——
    少一个字段，前端拿到的就是 undefined"""
    _enqueue()

    empty = queue.empty_result()
    full = queue.flush(FakeClient([None]))

    assert set(empty) == set(full)
