"""日报推送测试：不连真 NapCat，mock requests.post 验证重试/跳过/状态落盘"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import requests

from bot import report as report_mod
from server.app.config import DATA_DIR, Config


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def napcat_cfg(monkeypatch):
    """配好一套假 NapCat；重试间隔压成 0 让测试不真睡"""
    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "http://napcat.test:3000")
    monkeypatch.setattr(Config, "NAPCAT_TOKEN", "test-token")
    monkeypatch.setattr(Config, "REPORT_GROUP_ID", "123456789")
    monkeypatch.setattr(report_mod, "PUSH_BACKOFF_S", (0, 0))


def _patch_post(monkeypatch, handler):
    monkeypatch.setattr(requests, "post", handler)


def test_push_success_first_try(napcat_cfg, monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, headers))
        return _Resp(200)

    _patch_post(monkeypatch, fake_post)
    ok, msg = report_mod.push_to_group("测试日报内容")
    assert ok is True
    assert msg == "推送成功"
    assert len(calls) == 1
    url, headers = calls[0]
    assert url == "http://napcat.test:3000/send_group_msg"
    assert headers["Authorization"] == "Bearer test-token"


def test_push_retries_then_succeeds(napcat_cfg, monkeypatch):
    """前两次失败第三次成功：必须重试且最终报成功"""
    seq = [_Resp(500, "boom"), _Resp(502, "bad gateway"), _Resp(200)]

    def fake_post(url, json=None, headers=None, timeout=None):
        return seq.pop(0)

    _patch_post(monkeypatch, fake_post)
    ok, msg = report_mod.push_to_group("测试日报内容")
    assert ok is True, msg
    assert not seq  # 三次全用掉


def test_push_fails_after_all_attempts(napcat_cfg, monkeypatch):
    """一直连不上：重试满 3 次后报失败，消息里带重试次数"""
    attempts = []

    def fake_post(url, json=None, headers=None, timeout=None):
        attempts.append(url)
        raise requests.RequestException("connection refused")

    _patch_post(monkeypatch, fake_post)
    ok, msg = report_mod.push_to_group("测试日报内容")
    assert ok is False
    assert len(attempts) == report_mod.PUSH_ATTEMPTS == 3
    assert "已重试 3 次" in msg


def test_push_skipped_when_unconfigured(monkeypatch):
    """没配 webhook：直接跳过，一次 HTTP 都不发"""
    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "")

    def fake_post(url, **kwargs):
        raise AssertionError("未配置时不应发请求")

    _patch_post(monkeypatch, fake_post)
    ok, msg = report_mod.push_to_group("x")
    assert ok is False
    assert "未配置" in msg


def test_push_skipped_when_no_group_id(napcat_cfg, monkeypatch):
    """配了 webhook 但没配群号：跳过，不发请求"""
    monkeypatch.setattr(Config, "REPORT_GROUP_ID", "")

    def fake_post(url, **kwargs):
        raise AssertionError("缺少群号时不应发请求")

    _patch_post(monkeypatch, fake_post)
    ok, msg = report_mod.push_to_group("x")
    assert ok is False
    assert "REPORT_GROUP_ID" in msg


def test_save_push_status_roundtrip():
    day = date(2026, 10, 21)
    path = report_mod.save_push_status(day, {
        "date": day.isoformat(), "pushed": True,
        "message": "推送成功", "pushed_at": "2026-10-21T18:00:01",
    })
    assert path.name == "2026-10-21.status.json"
    assert not list(path.parent.glob("*.tmp"))  # 原子写不留临时文件
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pushed"] is True
    assert data["message"] == "推送成功"
    assert "updated_at" in data

    # 覆盖更新（补推后状态刷新）
    report_mod.save_push_status(day, {"date": day.isoformat(), "pushed": False, "message": "失败"})
    assert json.loads(path.read_text(encoding="utf-8"))["pushed"] is False


def test_run_daily_persists_status(app, monkeypatch):
    """run_daily 全流程：生成 + 存档 + 推送状态落盘"""
    monkeypatch.setattr(report_mod, "push_to_group", lambda content: (True, "推送成功"))
    day = date(2026, 10, 21)

    result = report_mod.run_daily(app, day, push=True)

    assert result["pushed"] is True
    assert result["date"] == "2026-10-21"
    assert Path(result["path"]).exists()  # 日报 Markdown 已存档

    status_file = DATA_DIR / "reports" / "2026-10-21.status.json"
    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert data["pushed"] is True
    assert data["pushed_at"] is not None


def test_run_daily_records_failure_status(app, monkeypatch):
    """推送失败：状态落盘记下失败原因，日报本身不丢"""
    monkeypatch.setattr(report_mod, "push_to_group", lambda content: (False, "推送异常: 超时"))
    day = date(2026, 10, 20)

    result = report_mod.run_daily(app, day, push=True)

    assert result["pushed"] is False
    data = json.loads((DATA_DIR / "reports" / "2026-10-20.status.json").read_text(encoding="utf-8"))
    assert data["pushed"] is False
    assert "超时" in data["message"]
    assert data["pushed_at"] is None
