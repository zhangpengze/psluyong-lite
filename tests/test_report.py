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


# ---------- 日报正文结构 ----------

_DAY = date(2026, 10, 21)

# 自洽数据集：4 个伙伴共 25 条，每个伙伴的模板分布明确
_DATASET = [
    ("农技推广中心", "农业加急", 3),
    ("农技推广中心", "今日农业", 4),
    ("农业大学", "中国收藏", 4),
    ("农业大学", "市场导报", 3),
    ("农业科学院", "科学导报", 3),
    ("农业科学院", "知网体育时空", 3),
    ("园艺研究所", "中国烹饪", 3),
    ("园艺研究所", "维普体育时空", 2),
]


def _seed_records(app, rows):
    from server.app.db import db
    from server.app.models import Record

    with app.app_context():
        i = 0
        for partner, template, count in rows:
            for _ in range(count):
                db.session.add(Record(
                    work_date=_DAY,
                    employee_name="测试员",
                    template_name=template,
                    partner_name=partner,
                    author=f"作者{i}",
                    title=f"标题{i}",
                    status="success",
                    processing_ms=1000,
                ))
                i += 1
        db.session.commit()


def _partner_blocks(seg: str) -> dict:
    """把「按合作伙伴」段落解析成 {伙伴: {total, templates}}，不依赖 SQL 返回顺序"""
    import re

    blocks: dict[str, dict] = {}
    current = None
    for line in seg.splitlines():
        m = re.match(r"  · (.+)：(\d+) 条$", line)
        if m:
            current = m.group(1)
            blocks[current] = {"total": int(m.group(2)), "templates": []}
        elif current is not None:
            t = re.match(r"\s+- (.+)：(\d+) 条$", line)
            if t:
                blocks[current]["templates"].append((t.group(1), int(t.group(2))))
            elif t is None and "等" in line:  # 截断汇总行不计入分布
                pass
    return blocks


def test_report_partner_section_shows_template_breakdown(app, clean_db):
    """合作伙伴段落：每个伙伴下面展开其模板分布，总数与分布自洽"""
    _seed_records(app, _DATASET)
    content = report_mod.build_daily_report(app, _DAY)

    # 按员工段落已按需求移除
    assert "按员工" not in content

    # 按模板段落保持原逻辑（Top 降序）
    assert "按模板：" in content
    assert "  · 中国收藏：4 条" in content

    seg = content.split("按合作伙伴：")[1].split("\n\n")[0]
    blocks = _partner_blocks(seg)

    assert set(blocks) == {"农技推广中心", "农业大学", "农业科学院", "园艺研究所"}
    assert blocks["农技推广中心"]["total"] == 7
    assert sorted(blocks["农技推广中心"]["templates"]) == [("今日农业", 4), ("农业加急", 3)]
    assert sorted(blocks["农业大学"]["templates"]) == [("中国收藏", 4), ("市场导报", 3)]
    # 每个伙伴的分布之和必须等于其总数（不重不漏）
    for name, blk in blocks.items():
        assert sum(c for _, c in blk["templates"]) == blk["total"], name


def test_report_partner_breakdown_unfilled_template(app, clean_db):
    """模板为空的记录归「未填模板」，分布之和仍等于伙伴总数"""
    from server.app.db import db
    from server.app.models import Record

    with app.app_context():
        for i in range(2):
            db.session.add(Record(
                work_date=_DAY, employee_name="测试员", template_name="",
                partner_name="园艺研究所", author=f"空{i}", title=f"题{i}",
                status="success", processing_ms=1,
            ))
        db.session.commit()

    seg = report_mod.build_daily_report(app, _DAY).split("按合作伙伴：")[1].split("\n\n")[0]
    assert "· 园艺研究所：2 条" in seg
    assert "- 未填模板：2 条" in seg


def test_report_partner_breakdown_truncates_long_tail(app, clean_db, monkeypatch):
    """模板超过 PARTNER_TEMPLATE_LIMIT 的伙伴只列前 N 个 + 汇总一行"""
    monkeypatch.setattr(report_mod, "PARTNER_TEMPLATE_LIMIT", 3)
    rows = [("大户伙伴", f"模板{i}", 1) for i in range(6)]
    _seed_records(app, rows)

    seg = report_mod.build_daily_report(app, _DAY).split("按合作伙伴：")[1].split("\n\n")[0]
    assert "· 大户伙伴：6 条" in seg
    assert "- 模板0：1 条" in seg
    assert "- 模板2：1 条" in seg
    assert "- 模板3：1 条" not in seg
    assert "- 等 3 个模板" in seg


# ---------- 区间日报 ----------

def _seed_range(app, rows):
    """rows: [(day, partner, template, count)]"""
    from server.app.db import db
    from server.app.models import Record

    with app.app_context():
        i = 0
        for day, partner, template, count in rows:
            for _ in range(count):
                db.session.add(Record(
                    work_date=day, employee_name="测试员", template_name=template,
                    partner_name=partner, author=f"作者{i}", title=f"标题{i}",
                    status="success", processing_ms=1,
                ))
                i += 1
        db.session.commit()


def test_period_report_includes_by_day_section(app, clean_db):
    """区间版：标题带区间、多「按日」段、合计口径与单日一致"""
    _seed_range(app, [
        (date(2026, 10, 19), "农技推广中心", "农业加急", 2),
        (date(2026, 10, 20), "农技推广中心", "农业加急", 3),
        (date(2026, 10, 20), "农业大学", "中国收藏", 4),
        (date(2026, 10, 21), "农业大学", "中国收藏", 1),
    ])
    content = report_mod.build_period_report(app, date(2026, 10, 19), date(2026, 10, 21))

    assert "【2026-10-19 ~ 2026-10-21 期刊录用汇总】" in content
    assert "总录用：10 条" in content
    # 按日拆分：倒序列出每天条数（10-21 只有 1 条）
    by_day = content.split("按日：")[1].split("\n\n")[0]
    assert "· 10-21：1 条" in by_day
    assert "· 10-20：7 条" in by_day
    assert "· 10-19：2 条" in by_day
    # 伙伴分布是区间合计（农业加急 5 条 = 2+3）
    seg = content.split("按合作伙伴：")[1].split("\n\n")[0]
    assert "· 农技推广中心：5 条" in seg
    assert "- 农业加急：5 条" in seg


def test_period_report_swaps_reversed_range(app, clean_db):
    """start > end 自动交换，不报错"""
    _seed_range(app, [(date(2026, 10, 20), "农业大学", "中国收藏", 3)])
    content = report_mod.build_period_report(app, date(2026, 10, 21), date(2026, 10, 20))
    assert "【2026-10-20 ~ 2026-10-21 期刊录用汇总】" in content
    assert "总录用：3 条" in content


def test_daily_report_unchanged_shape(app, clean_db):
    """单日版保持原契约：标题、无按日段"""
    _seed_records(app, _DATASET)
    content = report_mod.build_daily_report(app, _DAY)
    assert "【2026-10-21 期刊录用日报】" in content
    assert "按日：" not in content
    assert "总录用：25 条" in content


# ---------- 失败即时告警 ----------

@pytest.fixture
def alert_cfg(monkeypatch):
    """配好 NapCat 并清空告警门控"""
    report_mod.reset_alert_throttle()
    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "http://napcat.test:3000")
    monkeypatch.setattr(Config, "NAPCAT_TOKEN", "test-token")
    monkeypatch.setattr(Config, "REPORT_GROUP_ID", "123456789")
    monkeypatch.setattr(Config, "ALERT_MIN_INTERVAL_S", 300)
    yield
    report_mod.reset_alert_throttle()


def test_push_alert_sends_to_group(alert_cfg, monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json))
        return _Resp(200)

    monkeypatch.setattr(requests, "post", fake_post)
    ok, msg = report_mod.push_alert("⚠️ 录用失败\n员工：张三")
    assert ok is True
    assert calls[0][0] == "http://napcat.test:3000/send_group_msg"
    assert calls[0][1]["group_id"] == 123456789
    assert "录用失败" in calls[0][1]["message"][0]["data"]["text"]


def test_push_alert_throttles_second_call(alert_cfg, monkeypatch):
    """间隔内第二条告警被抑制——PS 故障批量失败不会刷爆群"""
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(200))

    assert report_mod.push_alert("第一条")[0] is True
    ok, msg = report_mod.push_alert("第二条")
    assert ok is False
    assert "已抑制" in msg


def test_push_alert_skipped_when_unconfigured(monkeypatch):
    """没配 NapCat：静默跳过，不发请求"""
    report_mod.reset_alert_throttle()
    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "")

    def fake_post(url, **kw):
        raise AssertionError("未配置时不应发请求")

    monkeypatch.setattr(requests, "post", fake_post)
    ok, msg = report_mod.push_alert("x")
    assert ok is False
    assert "未配置" in msg
