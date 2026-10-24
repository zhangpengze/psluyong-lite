"""运维告警节流：按 (kind, title) 分开计时 + 被抑制的必须留痕。

为什么单开一个文件：这一条 bug 的表现是"少了两条告警"，而不是报错。
`nightly_job` 一次跑备份 → 镜像 → 磁盘三步，三步都失败时是三条互不相干的
告警；用全局单哨兵时只有第一条能出去，后两条连日志都没有——凌晨最该知道的
"图没镜像"就这么没了，而且事后完全查不出来。
"""

from __future__ import annotations

import json

import pytest
import requests

from bot import report_alert as alert_mod
from server.app.config import Config


class _Resp:
    status_code = 200

    def json(self):
        return {"status": "ok", "retcode": 0}


@pytest.fixture()
def ops_cfg(monkeypatch, tmp_path):
    """配好推送通道 + 把本地告警流水指到临时目录"""
    from server.app import config as config_mod

    alert_mod.reset_ops_alert_throttle()
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(Config, "ALERT_ON_OPS_FAILURE", True)
    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "http://napcat.test:3000")
    monkeypatch.setattr(Config, "NAPCAT_TOKEN", "t")
    monkeypatch.setattr(Config, "REPORT_GROUP_ID", "123456789")
    yield tmp_path
    alert_mod.reset_ops_alert_throttle()


def _sent_texts(monkeypatch) -> list[str]:
    calls: list[str] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json["message"][0]["data"]["text"])
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def test_three_different_alerts_in_one_window_all_go_out(ops_cfg, monkeypatch):
    """夜维三步同时失败：三条都得发出去。这是本节流器存在的全部理由。"""
    sent = _sent_texts(monkeypatch)

    results = [
        alert_mod.push_ops_alert("数据库备份失败", "OSError: disk full", kind="backup"),
        alert_mod.push_ops_alert("图片镜像失败", "PermissionError", kind="mirror"),
        alert_mod.push_ops_alert("成品图占用已超阈值", "42 GB ≥ 40", kind="disk"),
    ]

    assert [ok for ok, _ in results] == [True, True, True], "不同主题的告警不许互相挤占"
    assert len(sent) == 3
    assert "数据库备份失败" in sent[0] and "成品图占用已超阈值" in sent[2]


def test_same_alert_within_window_is_suppressed_and_logged(ops_cfg, monkeypatch, caplog):
    sent = _sent_texts(monkeypatch)
    assert alert_mod.push_ops_alert("数据库备份失败", "A", kind="backup")[0] is True

    with caplog.at_level("WARNING", logger="bot.report_alert"):
        ok, msg = alert_mod.push_ops_alert("数据库备份失败", "B", kind="backup")

    assert ok is False and "已抑制" in msg and "第 1 次" in msg
    assert len(sent) == 1
    assert "运维告警已抑制" in caplog.text, "被压掉的告警必须留下日志，否则事后查不出少了什么"


def test_same_title_different_kind_are_independent(ops_cfg, monkeypatch):
    """同标题不同领域也要分开——比如"任务失败"在备份和镜像里是两件事"""
    sent = _sent_texts(monkeypatch)
    assert alert_mod.push_ops_alert("同步失败", "x", kind="backup")[0] is True
    assert alert_mod.push_ops_alert("同步失败", "x", kind="mirror")[0] is True
    assert len(sent) == 2


def test_suppression_counter_resets_after_send(ops_cfg, monkeypatch):
    _sent_texts(monkeypatch)
    alert_mod.push_ops_alert("T", "1", kind="k")
    alert_mod.push_ops_alert("T", "2", kind="k")
    # 把上次发送时刻推到窗口之外
    alert_mod._ops_last_sent[("k", "T")] -= alert_mod.OPS_ALERT_MIN_INTERVAL_S + 1
    assert alert_mod.push_ops_alert("T", "3", kind="k")[0] is True
    assert ("k", "T") not in alert_mod._ops_suppressed, "恢复发送后计数要清掉"


def test_local_alert_file_records_every_state(ops_cfg, monkeypatch):
    """本地流水：NapCat 挂了群消息全静默，这份文件是唯一的痕迹"""
    _sent_texts(monkeypatch)
    alert_mod.push_ops_alert("备份失败", "detail", kind="backup")
    alert_mod.push_ops_alert("备份失败", "again", kind="backup")

    monkeypatch.setattr(Config, "ALERT_ON_OPS_FAILURE", False)
    alert_mod.push_ops_alert("已关闭的告警", "", kind="backup")
    monkeypatch.setattr(Config, "ALERT_ON_OPS_FAILURE", True)
    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "")
    alert_mod.push_ops_alert("没通道的告警", "", kind="backup")

    lines = [json.loads(x) for x in (ops_cfg / "logs" / "ops-alerts.jsonl").read_text("utf-8").splitlines()]
    states = [ln["state"] for ln in lines]
    assert states[0] == "sent"
    assert states[1].startswith("suppressed#")
    assert "disabled" in states and "unconfigured" in states
    assert all(ln["title"] for ln in lines)


def test_local_alert_write_failure_does_not_raise(ops_cfg, monkeypatch):
    from server.app import config as config_mod

    _sent_texts(monkeypatch)
    monkeypatch.setattr(config_mod, "DATA_DIR", ops_cfg / "no" / "such" / "dir" / "\0bad")
    ok, _ = alert_mod.push_ops_alert("T", "d", kind="k")
    assert ok is True, "写不了本地流水也必须把告警发出去"


def test_disabled_switch_short_circuits(ops_cfg, monkeypatch):
    monkeypatch.setattr(Config, "ALERT_ON_OPS_FAILURE", False)
    called = []
    monkeypatch.setattr(requests, "post", lambda *a, **kw: called.append(1) or _Resp())
    ok, msg = alert_mod.push_ops_alert("T", "d")
    assert ok is False and "已关闭" in msg and called == []
