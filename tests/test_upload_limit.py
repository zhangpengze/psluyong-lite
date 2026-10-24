"""录用记录上传的限流（`upload_guard` + `ratelimit.upload_allowed`）

一条记录可以带 64MB 成品图。旧实现这条路上什么都没有：一个写错的循环、一个被
捡到的 API Key、或者从旧机器重放的脚本，几分钟就能把 NAS 的盘写满——而业务量
本来只是"员工出一张图提交一次"。

三条要求，缺一条这个限流就是装饰品：
  1. 挡住之后必须**可重试**（429，不是 400）——sidecar 的队列只删 400/409/422，
     429 会留着下一轮；写成 400 就等于被限流一次丢一条记录。
  2. 必须在**读请求体之前**挡（否则先替对方把 64MB 吃进内存/临时盘）。
  3. 单来源一道、全站一道：只按来源分，10 台机器同时发疯照样打满。
"""

from __future__ import annotations

from collections import defaultdict, deque
from io import BytesIO

import pytest

from server.app import ratelimit
from tests.conftest import API_KEY

AUTH = {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    monkeypatch.setattr(ratelimit, "_HITS", defaultdict(deque))
    yield


def _submit(client, make_image, ip="10.0.0.1", title="标题"):
    return client.post(
        "/api/v1/records",
        data={
            "employee_name": "张三",
            "template_name": "录用通知模板",
            "author": "李四",
            "title": title,
            "status": "success",
            "image": (make_image(), "result.png"),
        },
        headers=AUTH,
        content_type="multipart/form-data",
        environ_base={"REMOTE_ADDR": ip},
    )


def test_upload_rejected_after_quota(client, clean_db, sample_image, monkeypatch):
    monkeypatch.setattr(ratelimit, "UPLOAD_PER_SOURCE", 2)

    assert _submit(client, sample_image, title="一").status_code == 201
    assert _submit(client, sample_image, title="二").status_code == 201
    resp = _submit(client, sample_image, title="三")

    assert resp.status_code == 429
    body = resp.get_json()
    assert body["retryable"] is True, "429 必须标记可重试，否则客户端会当业务拒绝删掉记录"
    assert int(resp.headers["Retry-After"]) >= 1


def test_quota_is_per_source(client, clean_db, sample_image, monkeypatch):
    """一个员工把额度用光，不能连累别人——按来源分桶就是为这个"""
    monkeypatch.setattr(ratelimit, "UPLOAD_PER_SOURCE", 1)

    assert _submit(client, sample_image, ip="10.0.0.9", title="一").status_code == 201
    assert _submit(client, sample_image, ip="10.0.0.9", title="二").status_code == 429

    assert _submit(client, sample_image, ip="10.0.0.8", title="三").status_code == 201


def test_global_quota_blocks_even_fresh_sources(client, clean_db, sample_image, monkeypatch):
    """全站那道闸：只按来源限流时，10 台机器各发 30 次/分照样打满磁盘"""
    monkeypatch.setattr(ratelimit, "UPLOAD_GLOBAL", 2)

    assert _submit(client, sample_image, ip="10.0.0.1", title="一").status_code == 201
    assert _submit(client, sample_image, ip="10.0.0.2", title="二").status_code == 201
    assert _submit(client, sample_image, ip="10.0.0.3", title="三").status_code == 429


def test_rejected_upload_never_reads_the_body(client, app, clean_db, sample_image, monkeypatch):
    """限流必须在解析 multipart 之前生效

    证据：先把额度用光，再把 body 上限压到 512 字节，发一个 2KB 的请求——
    先解析的做法会抛 413（上传超限），先限流的做法给 429。
    """
    monkeypatch.setattr(ratelimit, "UPLOAD_PER_SOURCE", 1)
    assert _submit(client, sample_image, title="一").status_code == 201

    monkeypatch.setitem(app.config, "MAX_CONTENT_LENGTH", 512)
    resp = client.post(
        "/api/v1/records",
        data=b"x" * 2048,
        headers={**AUTH, "Content-Type": "multipart/form-data; boundary=x"},
        environ_base={"REMOTE_ADDR": "10.0.0.1"},
    )

    assert resp.status_code == 429, "被限流时应先给 429；413 说明请求体已经被解析了"


def test_body_over_limit_is_413_when_quota_is_available(client, app, sample_image, monkeypatch):
    """对照组：额度够时，超限的 body 走的是 413

    没有这一条，上面那条"429 而不是 413"就可能只是碰巧（比如 413 根本不触发），
    永远抓不到"限流挪到 request.form 之后"这个回归。
    """
    monkeypatch.setitem(app.config, "MAX_CONTENT_LENGTH", 512)

    resp = client.post(
        "/api/v1/records",
        data=b"x" * 2048,
        headers={**AUTH, "Content-Type": "multipart/form-data; boundary=x"},
        environ_base={"REMOTE_ADDR": "10.0.0.7"},
    )

    assert resp.status_code == 413


def test_upload_guard_does_not_limit_reads(client):
    """列表接口不该被上传限流牵连（否则员工看记录都要被限速）"""
    for _ in range(5):
        assert client.get("/api/v1/records", headers=AUTH).status_code == 200
