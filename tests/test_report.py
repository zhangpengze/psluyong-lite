"""日报推送测试：不连真 NapCat，mock requests.post 验证重试/跳过/状态落盘"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import requests

from bot import report as report_mod
from server.app.config import Config


@pytest.fixture()
def isolated_data_dir(monkeypatch, tmp_path):
    """把落盘根目录指到本用例的 tmp。

    run_daily 会往共享 DATA_DIR/reports 写 .md/.status.json，污染
    test_bot_handler 对「纯查询不落盘」的断言——子集/随机顺序下会红。
    """
    import server.app.config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    return tmp_path


class _Resp:
    """假响应。json_body 为 None 时 .json() 抛 ValueError，模拟"不是 JSON"。

    必须支持 body：OneBot 的失败语义在响应体里，只给 status_code 的假对象
    会让"HTTP 200 但 body 失败"这类真事故永远测不出来。
    """

    def __init__(self, status_code: int, text: str = "", json_body=None):
        self.status_code = status_code
        self.text = text
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("Response is not JSON")
        return self._json


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


def test_push_ok_http_but_failed_body_is_failure(napcat_cfg, monkeypatch):
    """HTTP 200 + {"status":"failed"} 必须判失败（真事故形态）

    群号写错 / 机器人被踢出群 / Token 轮换后没同步 / 被风控 / 消息超长，
    OneBot 全部回 200。以前只看状态码，于是日志、徽标、仪表盘一起绿，
    群里一条消息都没有，兜底告警也永远不会触发。
    """
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        return _Resp(200, json_body={"status": "failed", "retcode": 100, "message": "GROUP_NOT_FOUND"})

    _patch_post(monkeypatch, fake_post)
    ok, msg = report_mod.push_to_group("测试日报内容")

    assert ok is False, "HTTP 200 但 body 说失败，不能报成功"
    assert "GROUP_NOT_FOUND" in msg and "100" in msg
    assert len(calls) == 1, "body 里是业务拒绝，重发同样失败，不该重试"


def test_push_ok_http_nonzero_retcode_is_failure(napcat_cfg, monkeypatch):
    """status 看着正常但 retcode≠0：仍判失败（部分实现只填 retcode）"""

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(200, json_body={"status": "ok", "retcode": 1404, "msg": "群不存在"})

    _patch_post(monkeypatch, fake_post)
    ok, msg = report_mod.push_to_group("x")

    assert ok is False
    assert "1404" in msg and "群不存在" in msg


def test_push_4xx_not_retried(napcat_cfg, monkeypatch):
    """4xx 是请求本身被拒（Token 错/群号写错），重试只会让人晚 20 秒才知道"""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        return _Resp(403, "forbidden")

    _patch_post(monkeypatch, fake_post)
    ok, msg = report_mod.push_to_group("x")

    assert ok is False
    assert len(calls) == 1
    assert "不重试" in msg


def test_run_daily_records_not_pushed_when_body_failed(app, isolated_data_dir, napcat_cfg, monkeypatch):
    """推送状态文件必须跟着真实结果走：body 失败就不能写「已推送」"""

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(200, json_body={"status": "failed", "retcode": 1200, "message": "风控拦截"})

    _patch_post(monkeypatch, fake_post)
    day = date(2026, 10, 19)

    result = report_mod.run_daily(app, day, push=True)

    assert result["pushed"] is False
    status_file = isolated_data_dir / "reports" / "2026-10-19.status.json"
    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert data["pushed"] is False
    assert "风控拦截" in data["message"]
    assert Path(result["path"]).exists(), "推送失败不影响日报存档"


def test_manual_run_without_push_keeps_pushed_badge(admin_client, admin_csrf, isolated_data_dir):
    """「只生成不推送」不能把定时任务推过的「已推送」徽标刷成未推送"""
    day = date(2026, 10, 18)
    report_mod.save_push_status(day, {
        "date": day.isoformat(), "pushed": True,
        "message": "推送成功", "pushed_at": "2026-10-18T18:00:01",
    })

    resp = admin_client.post(
        "/api/v1/reports/run", json={"date": day.isoformat(), "push": False}, headers=admin_csrf
    )

    assert resp.status_code == 200
    data = json.loads((isolated_data_dir / "reports" / "2026-10-18.status.json").read_text(encoding="utf-8"))
    assert data["pushed"] is True, "只生成不推送把历史徽标覆盖了"
    assert data["message"] == "推送成功"


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


def test_run_daily_persists_status(app, monkeypatch, isolated_data_dir):
    """run_daily 全流程：生成 + 存档 + 推送状态落盘"""
    monkeypatch.setattr(report_mod, "push_to_group", lambda content: (True, "推送成功"))
    day = date(2026, 10, 21)

    result = report_mod.run_daily(app, day, push=True)

    assert result["pushed"] is True
    assert result["date"] == "2026-10-21"
    assert Path(result["path"]).exists()  # 日报 Markdown 已存档

    status_file = isolated_data_dir / "reports" / "2026-10-21.status.json"
    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert data["pushed"] is True
    assert data["pushed_at"] is not None


def test_run_daily_records_failure_status(app, monkeypatch, isolated_data_dir):
    """推送失败：状态落盘记下失败原因，日报本身不丢"""
    monkeypatch.setattr(report_mod, "push_to_group", lambda content: (False, "推送异常: 超时"))
    day = date(2026, 10, 20)

    result = report_mod.run_daily(app, day, push=True)

    assert result["pushed"] is False
    data = json.loads((isolated_data_dir / "reports" / "2026-10-20.status.json").read_text(encoding="utf-8"))
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


def test_push_alert_not_swallowed_right_after_boot(alert_cfg, monkeypatch):
    """开机/容器启动不足一个窗口时，首条告警仍必须发出去。

    time.monotonic() 的起点是开机（容器里是容器启动），不是 Unix 纪元：
    CI runner、刚 docker compose up 的 NAS 上该值可能只有几十秒。若用 0.0
    当"从未告警"哨兵，now - 0.0 < interval 恒成立，首条告警被当成重复而静默
    丢弃——机器刚重启恰恰是最需要收到告警的时刻（真实踩过：CI 两平台齐红）。
    """
    from bot import report_alert as alert_mod

    monkeypatch.setattr(alert_mod.time, "monotonic", lambda: 5.0)  # 开机 5 秒
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(200))
    alert_mod.reset_alert_throttle()

    ok, msg = report_mod.push_alert("首条告警")
    assert ok is True, f"开机 5s 时首条告警被抑制：{msg}"

    # 同一窗口内第二条仍要被抑制——防呆不能把节流本身改坏
    ok2, msg2 = report_mod.push_alert("第二条告警")
    assert ok2 is False
    assert "已抑制" in msg2


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


# ---------- 正文长度上限（QQ 单条消息） ----------
# 伙伴数 × 模板名（200 字符）能轻易把日报撑到上万字符，而 OneBot 对超长文本
# 是整条拒发（retcode 100）。那种失败会被 P0-1 的 body 校验判成"推送失败"，
# 于是最忙的那天（数据最多、最该报出去的那天）群里一条消息都没有。
# 处理方式：只截"要发出去的那份"，存档的 .md 永远是全文。


def _long_report(lines: int = 200) -> str:
    """每行都带中文（3 字节/字），保证 200 行一定超过上限"""
    return "\n".join(f"  · 合作伙伴{i}：模板{i}：{i} 条" for i in range(lines))


def test_clamp_leaves_short_text_alone():
    from bot.report_render import clamp_for_message

    text = "【2026-10-24 期刊录用日报】\n总录用：3 条"

    assert clamp_for_message(text) == (text, 0)


def test_clamp_counts_bytes_not_characters():
    """中文一个字 3 字节：按字符数判断长度会以为还很空，实际早就超了"""
    from bot.report_render import MESSAGE_MAX_BYTES, clamp_for_message

    text = "中" * 2000  # len() == 2000 < 4000，但 bytes == 6000
    assert len(text) < MESSAGE_MAX_BYTES < len(text.encode("utf-8"))

    clamped, dropped = clamp_for_message(text)

    assert dropped == 1
    assert clamped != text


def test_clamp_keeps_whole_lines():
    from bot.report_render import clamp_for_message

    original = _long_report(300)
    clamped, dropped = clamp_for_message(original)

    assert dropped > 0
    body = clamped.splitlines()
    tail = body[-1]
    assert "已省略" in tail and "完整版见后台" in tail
    for line in body[:-1]:
        assert line in original, f"截断把一行切坏了：{line!r}"


def test_push_sends_the_clamped_text(napcat_cfg, monkeypatch):
    """真正发出去的那份必须是截断后的：只算不截等于没做"""
    from bot.report_render import MESSAGE_MAX_BYTES

    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _Resp(200)

    _patch_post(monkeypatch, fake_post)
    ok, _ = report_mod.push_to_group(_long_report(300))

    assert ok is True
    text = sent[0]["message"][0]["data"]["text"]
    assert len(text.encode("utf-8")) <= MESSAGE_MAX_BYTES + 200, "还是超了上限"
    assert "已省略" in text


def test_run_daily_pushes_clamped_but_archives_full(app, isolated_data_dir, napcat_cfg, monkeypatch):
    """存档是全文、发送是截断——"完整版见后台"这句承诺得是真的"""
    from bot.report_render import MESSAGE_MAX_BYTES

    huge = _long_report(300)
    monkeypatch.setattr(report_mod, "render", lambda *a, **k: huge)
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _Resp(200)

    _patch_post(monkeypatch, fake_post)
    result = report_mod.run_daily(app, date(2026, 10, 24), push=True)

    archived = Path(result["path"]).read_text(encoding="utf-8")
    assert archived == huge, "存档必须是全文，否则「完整版见后台」是假的"
    pushed = sent[0]["message"][0]["data"]["text"]
    assert len(pushed.encode("utf-8")) < len(huge.encode("utf-8"))
    assert len(pushed.encode("utf-8")) <= MESSAGE_MAX_BYTES + 200
    assert result["content"] == huge, "接口返回的正文仍应是全文（后台页面要显示它）"


def test_clamp_payload_leaves_other_shapes_alone():
    """形状不对就原样放行：群里发图/at 的调用不走这里，绝不猜"""
    from bot.report_render import clamp_payload

    payload = {"group_id": 1, "message": [{"type": "image", "data": {"file": "x.jpg"}}]}

    assert clamp_payload(payload) == (payload, 0)
