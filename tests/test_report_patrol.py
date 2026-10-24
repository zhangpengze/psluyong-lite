"""日报幂等 + 次日终版巡检

覆盖两类"没人发现"的事故：
  1. 18:00 之后补录的记录进不了任何一份日报 → 次日终版补上，且**只在数据变了时**才发
  2. 18:00 服务没起来/推送失败 → 次日巡检发现昨天没推成功，补推

幂等闸门如果写错，两种坏法都很显眼：闸门太严 → 补录永远推不出终版；
闸门太松 → 群里每天早上多一条一模一样的日报。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from bot import report as report_mod
from bot import report_patrol as patrol_mod
from server.app.config import Config

DAY = date(2026, 10, 21)


@pytest.fixture()
def data_dir(monkeypatch, tmp_path):
    """日报与状态文件都落在本用例的 tmp 里"""
    import server.app.config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def sent(monkeypatch):
    """把群推送拦下来，只记录发了什么"""
    calls: list[str] = []

    def fake_push(content: str, group_id=None):
        calls.append(content)
        return True, "推送成功"

    monkeypatch.setattr(report_mod, "push_to_group", fake_push)
    return calls


@pytest.fixture()
def ops_alerts(monkeypatch):
    captured: list[tuple[str, str]] = []

    def fake_alert(title: str, detail: str = ""):
        captured.append((title, detail))
        return True, "已捕获"

    monkeypatch.setattr(patrol_mod, "push_ops_alert", fake_alert, raising=False)
    # patrol 里是函数内 import，要打在来源模块上
    from bot import report_alert

    monkeypatch.setattr(report_alert, "push_ops_alert", fake_alert)
    return captured


def _add_record(app, day: date, author: str = "作者1", title: str = "标题1") -> None:
    from server.app.db import db
    from server.app.models import Record

    with app.app_context():
        db.session.add(
            Record(
                work_date=day,
                employee_name="测试员",
                template_name="农业加急",
                partner_name="农技推广中心",
                author=author,
                title=title,
                status="success",
                processing_ms=1000,
            )
        )
        db.session.commit()


def _status(data_dir, day: date) -> dict:
    path = data_dir / "reports" / f"{day:%Y-%m-%d}.status.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ---------- 幂等闸门 ----------

def test_same_content_is_not_pushed_twice(app, clean_db, data_dir, sent):
    """同一天同一批数据跑两次：群里只该有一条（服务重启重跑/重复部署）"""
    _add_record(app, DAY)

    first = report_mod.run_daily(app, DAY, push=True)
    second = report_mod.run_daily(app, DAY, push=True)

    assert first["pushed"] is True and first["skipped"] is False
    assert second["skipped"] is True
    assert second["message"].startswith("内容与已推送版本一致")
    assert len(sent) == 1, "同样的内容不能发第二条"


def test_force_bypasses_gate(app, clean_db, data_dir, sent):
    """后台「重推此日报」是显式动作：内容没变也要真的再发一次"""
    _add_record(app, DAY)
    report_mod.run_daily(app, DAY, push=True)

    result = report_mod.run_daily(app, DAY, push=True, force=True)

    assert result["skipped"] is False
    assert len(sent) == 2


def test_failed_push_is_retried_next_run(app, clean_db, data_dir, monkeypatch):
    """上次失败 → 这次同样的内容也要重试（闸门只挡"已成功过"的）"""
    _add_record(app, DAY)
    monkeypatch.setattr(report_mod, "push_to_group", lambda content, group_id=None: (False, "推送异常: 超时"))
    report_mod.run_daily(app, DAY, push=True)

    sent: list[str] = []
    monkeypatch.setattr(
        report_mod, "push_to_group",
        lambda content, group_id=None: (sent.append(content) or (True, "推送成功")),
    )
    result = report_mod.run_daily(app, DAY, push=True)

    assert result["pushed"] is True and result["skipped"] is False
    assert len(sent) == 1


def test_status_records_attempts_and_error(app, clean_db, data_dir, monkeypatch):
    """失败次数与原因要落盘——出问题时能看出"重试过 3 次都失败"而不是第一次就错"""
    _add_record(app, DAY)
    monkeypatch.setattr(report_mod, "push_to_group", lambda content, group_id=None: (False, "群号不存在"))
    report_mod.run_daily(app, DAY, push=True)
    report_mod.run_daily(app, DAY, push=True)

    status = _status(data_dir, DAY)
    assert status["pushed"] is False
    assert status["attempts"] == 2
    assert "群号不存在" in status["last_error"]


# ---------- 次日终版 ----------

def test_patrol_pushes_final_when_late_records_added(app, clean_db, data_dir, sent):
    """18:00 快报之后又补录了记录：次日巡检必须把终版推出去"""
    _add_record(app, DAY, "作者1", "标题1")
    report_mod.run_daily(app, DAY, push=True)  # 18:00 快报
    assert "总录用：1 条" in sent[0] and "终版" not in sent[0]

    _add_record(app, DAY, "作者2", "标题2")  # 员工 19:30 补录
    result = patrol_mod.patrol(app, today=DAY + timedelta(days=1))

    assert result["pushed"] is True and result["skipped"] is False
    assert len(sent) == 2
    assert "（终版）" in sent[1]
    assert "总录用：2 条" in sent[1], "终版必须包含补录的那条"
    assert _status(data_dir, DAY)["final"] is True


def test_patrol_is_silent_when_nothing_changed(app, clean_db, data_dir, sent):
    """数据没变：一条消息都不发（否则每天早上群里多一份重复日报）"""
    _add_record(app, DAY)
    report_mod.run_daily(app, DAY, push=True)

    result = patrol_mod.patrol(app, today=DAY + timedelta(days=1))

    assert result["skipped"] is True
    assert len(sent) == 1, "内容没变就不该再推"
    assert _status(data_dir, DAY)["final"] is True, "没变也算终版核对过了"


def test_patrol_pushes_when_yesterday_never_pushed(app, clean_db, data_dir, sent):
    """18:00 服务没起来：昨天一条都没推，巡检必须补上（MemoryJobStore 补不了）"""
    _add_record(app, DAY)

    result = patrol_mod.patrol(app, today=DAY + timedelta(days=1))

    assert result["pushed"] is True and result["skipped"] is False
    assert len(sent) == 1
    assert _status(data_dir, DAY)["final"] is True
    assert result["missing"] == []


def test_patrol_reports_older_gaps_without_spamming_group(app, clean_db, data_dir, sent, ops_alerts):
    """更早的缺口只告警列出日期，不自动往群里补推（宕机一周不会淹群）"""
    today = DAY + timedelta(days=4)
    _add_record(app, today - timedelta(days=1))   # 昨天有数据 → 巡检会补推
    _add_record(app, today - timedelta(days=2))   # 前天有数据，但日报从没生成过

    result = patrol_mod.patrol(app, today=today)

    assert len(sent) == 1, "只补昨天，不能把更早的日期也推一遍"
    assert result["missing"] == [(today - timedelta(days=2)).isoformat()]
    assert len(ops_alerts) == 1
    title, detail = ops_alerts[0]
    assert "1 天" in title
    assert (today - timedelta(days=2)).isoformat() in detail
    assert "重推此日报" in detail


def test_missing_days_ignores_days_that_never_had_work(app, clean_db, data_dir):
    """没有记录、也没生成过日报的日子（还没部署/那天没开工）不算缺口"""
    quiet = DAY - timedelta(days=30)
    busy = DAY - timedelta(days=3)
    _add_record(app, busy)

    assert patrol_mod.missing_days(app, [quiet]) == []
    assert patrol_mod.missing_days(app, [busy]) == [busy.isoformat()]


def test_patrol_no_alert_when_history_is_complete(app, clean_db, data_dir, sent, ops_alerts):
    """往前看的每一天都推成功过：不发任何告警"""
    today = DAY + timedelta(days=3)
    for offset in (1, 2):
        day = today - timedelta(days=offset)
        _add_record(app, day)
        report_mod.run_daily(app, day, push=True, force=True)

    patrol_mod.patrol(app, today=today)

    assert ops_alerts == []


# ---------- 渲染口径 ----------

def test_final_title_is_distinguishable(app, clean_db, data_dir):
    """快报与终版标题必须不同：群里两条同日期消息内容不一样，得能解释"""
    _add_record(app, DAY)
    daily = report_mod.build_daily_report(app, DAY)
    final = report_mod.build_daily_report(app, DAY, final=True)

    assert "期刊录用日报】" in daily
    assert "（终版）" in final.splitlines()[0]
    assert "18:00 之后补录" in final


def test_fingerprint_ignores_render_time(app, clean_db, data_dir):
    """指纹只看数据：同一批数据渲染两次（生成时间不同）必须算同一版"""
    from bot.report_stats import _collect_stats, stats_fingerprint

    _add_record(app, DAY)
    stats = _collect_stats(app, DAY, DAY)
    assert stats_fingerprint(stats) == stats_fingerprint(_collect_stats(app, DAY, DAY))

    _add_record(app, DAY, "作者9", "标题9")
    assert stats_fingerprint(_collect_stats(app, DAY, DAY)) != stats_fingerprint(stats)


def test_config_exposes_patrol_keys():
    """三个新键必须挂在 Config 上（拼错名字不会报错，只会让巡检按 0 点跑）"""
    for name in ("REPORT_FINAL_HOUR", "REPORT_FINAL_MINUTE", "REPORT_MISS_LOOKBACK_DAYS"):
        assert isinstance(getattr(Config, name), int), name
