"""仪表盘概览接口：时间范围、KPI 口径、伙伴 × 模板聚合、缓存、权限、页面直出。

覆盖这些「不测就不知道哪天坏」的点：
  - 五档时间范围的边界（今天/昨天/近7天/近30天/自定义的起止与校验）
  - KPI 口径：调用含失败、成功率单列、环比对比的是前一个等长区间
  - 合作伙伴 × 模板二级聚合：模板数、各模板次数、占比合计 100
  - 软删除不进统计；空伙伴名不进伙伴维度但计入总量
  - 概览缓存：命中同一对象，记录写入后失效
  - html 片段转义：伙伴名里的 < > 不能原样进 innerHTML
  - 首屏服务端直出：主管打开页面就有数据，不等 JS
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import API_KEY

AUTH = {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def _clear_overview_cache():
    """概览有 15 秒缓存，不清就会串用例"""
    from server.app.overview import _CACHE

    _CACHE.clear()
    yield
    _CACHE.clear()


def _seed(app, rows):
    """rows: [(work_date, employee, template, partner, status)]

    作者/标题按行递增：records 有去重唯一索引
    (work_date, employee_name, template_name, author, title)，
    不编不同的作者标题，第二批同配置的种子直接撞 500。
    """
    from server.app.db import db
    from server.app.models import Record

    with app.app_context():
        for i, (work_date, employee, template, partner, status) in enumerate(rows):
            db.session.add(
                Record(
                    work_date=work_date,
                    employee_name=employee,
                    template_name=template,
                    partner_name=partner,
                    author=f"作者{i}",
                    title=f"标题{i}",
                    status=status,
                )
            )
        db.session.commit()


def _overview(client, **params):
    return client.get("/api/v1/stats/overview", query_string=params, headers=AUTH)


# ---------- 时间范围 ----------

def test_overview_requires_auth(client, clean_db):
    resp = client.get("/api/v1/stats/overview")
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_default_range_is_7d(client, clean_db, app):
    today = date.today()
    _seed(
        app,
        [
            (today, "张三", "模板A", "科学出版社", "success"),
            (today - timedelta(days=6), "张三", "模板A", "科学出版社", "success"),
            (today - timedelta(days=7), "张三", "模板A", "科学出版社", "success"),  # 出界
        ],
    )
    data = _overview(client).get_json()
    assert data["range"]["start"] == (today - timedelta(days=6)).isoformat()
    assert data["range"]["end"] == today.isoformat()
    assert data["range"]["preset"] == "7d"
    assert data["kpi"]["calls"] == 2  # 第 7 天前的那条不算


def test_range_presets(client, clean_db, app):
    today = date.today()
    yesterday = today - timedelta(days=1)
    _seed(
        app,
        [
            (today, "张三", "模板A", "伙伴甲", "success"),
            (yesterday, "张三", "模板A", "伙伴甲", "success"),
            (today - timedelta(days=10), "张三", "模板A", "伙伴甲", "success"),
            (today - timedelta(days=40), "张三", "模板A", "伙伴甲", "success"),
        ],
    )
    assert _overview(client, range="today").get_json()["kpi"]["calls"] == 1
    assert _overview(client, range="yesterday").get_json()["kpi"]["calls"] == 1
    assert _overview(client, range="7d").get_json()["kpi"]["calls"] == 2
    assert _overview(client, range="30d").get_json()["kpi"]["calls"] == 3


def test_custom_range(client, clean_db, app):
    today = date.today()
    start = today - timedelta(days=3)
    _seed(app, [(start, "张三", "模板A", "伙伴甲", "success"), (today, "张三", "模板A", "伙伴甲", "success")])
    data = _overview(client, range="custom", start=start.isoformat(), end=today.isoformat()).get_json()
    assert data["range"]["preset"] == "custom"
    assert data["kpi"]["calls"] == 2
    assert len(data["trend"]) == 4  # 含首尾共 4 天，中间补零


@pytest.mark.parametrize(
    "params",
    [
        {"range": "custom", "start": "", "end": "2026-01-01"},
        {"range": "custom", "start": "2026-13-99", "end": "2026-01-01"},
        {"range": "custom", "start": "2026-01-02", "end": "2026-01-01"},  # 起 > 止
        {"range": "custom", "start": "2020-01-01", "end": "2026-01-01"},  # 超 90 天
        {"range": "custom", "start": "2026-01-01", "end": "2999-01-01"},  # 未来
        {"range": "decade"},
    ],
)
def test_overview_rejects_bad_range(client, clean_db, params):
    resp = _overview(client, **params)
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


# ---------- KPI 口径 ----------

def test_kpi_counts_failures_and_rate(client, clean_db, app):
    today = date.today()
    _seed(
        app,
        [
            (today, "张三", "模板A", "伙伴甲", "success"),
            (today, "张三", "模板A", "伙伴甲", "success"),
            (today, "李四", "模板B", "伙伴甲", "failed"),
        ],
    )
    kpi = _overview(client, range="today").get_json()["kpi"]
    assert kpi["calls"] == 3          # 失败也算一次调用
    assert kpi["success"] == 2
    assert kpi["failed"] == 1
    assert kpi["success_rate"] == 66.7


def test_kpi_delta_uses_previous_equal_period(client, clean_db, app):
    today = date.today()
    # 当期 7 天每天 1 条；前一个等长周期（today-13 ~ today-7）每天 2 条
    rows = [(today - timedelta(days=i), "张三", "模板A", "伙伴甲", "success") for i in range(7)]
    rows += [
        (today - timedelta(days=d), "张三", "模板A", "伙伴甲", "success")
        for d in range(7, 14)
        for _ in range(2)
    ]
    _seed(app, rows)
    kpi = _overview(client, range="7d").get_json()["kpi"]
    assert kpi["calls"] == 7
    assert kpi["calls_prev"] == 14
    assert kpi["calls_delta_pct"] == -50.0


def test_delta_none_when_previous_period_empty(client, clean_db, app):
    today = date.today()
    _seed(app, [(today, "张三", "模板A", "伙伴甲", "success")])
    kpi = _overview(client, range="today").get_json()["kpi"]
    assert kpi["calls_delta_pct"] is None  # 前端显示"上一周期无数据"，不瞎算百分比


def test_partners_new_counts_first_ever(client, clean_db, app):
    today = date.today()
    _seed(
        app,
        [
            (today, "张三", "模板A", "老伙伴", "success"),
            (today, "张三", "模板A", "新伙伴", "success"),
        ],
    )
    kpi = _overview(client, range="today").get_json()["kpi"]
    assert kpi["partners_active"] == 2
    assert kpi["partners_total"] == 2
    assert kpi["partners_new"] == 2


# ---------- 合作伙伴 × 模板 ----------

def test_partner_template_breakdown(client, clean_db, app):
    today = date.today()
    _seed(
        app,
        [
            (today, "张三", "期刊模板A", "科学出版社", "success"),
            (today, "张三", "期刊模板A", "科学出版社", "success"),
            (today, "张三", "期刊模板B", "科学出版社", "success"),
            (today, "李四", "模板C", "中华医学会", "success"),
        ],
    )
    data = _overview(client, range="today").get_json()
    partners = data["partners"]
    assert [p["name"] for p in partners] == ["科学出版社", "中华医学会"]  # 按调用降序

    press = partners[0]
    assert press["templates_used"] == 2           # 用了 2 个不同模板
    assert press["calls"] == 3
    assert press["share"] == 75.0
    assert press["status"] == "active"
    tpls = {t["name"]: t["calls"] for t in press["templates"]}
    assert tpls == {"期刊模板A": 2, "期刊模板B": 1}  # 每个模板用了几次
    assert sum(t["share"] for t in press["templates"]) == 100.0


def test_partner_status_dormant(client, clean_db, app):
    today = date.today()
    _seed(app, [(today - timedelta(days=20), "张三", "模板A", "沉睡伙伴", "success")])
    partner = _overview(client, range="30d").get_json()["partners"][0]
    assert partner["status"] == "dormant"
    assert partner["status_label"] == "停用"


def test_soft_deleted_excluded(client, clean_db, app):
    from server.app.db import db
    from server.app.models import Record

    today = date.today()
    _seed(app, [(today, "张三", "模板A", "伙伴甲", "success"), (today, "张三", "模板A", "伙伴甲", "success")])
    with app.app_context():
        Record.query.filter_by(work_date=today).first().is_deleted = True
        db.session.commit()
    assert _overview(client, range="today").get_json()["kpi"]["calls"] == 1


def test_empty_partner_not_in_partners_but_counted(client, clean_db, app):
    today = date.today()
    _seed(app, [(today, "张三", "模板A", "", "success")])
    data = _overview(client, range="today").get_json()
    assert data["kpi"]["calls"] == 1        # 计入总量
    assert data["partners"] == []           # 不进伙伴维度


def test_trend_fills_zero_days(client, clean_db, app):
    today = date.today()
    _seed(app, [(today, "张三", "模板A", "伙伴甲", "success")])
    trend = _overview(client, range="7d").get_json()["trend"]
    assert len(trend) == 7
    assert trend[0]["count"] == 0 and trend[-1]["count"] == 1  # 中间补零，图才连续


# ---------- 缓存 ----------

def test_cache_hit_then_invalidate_on_create(client, clean_db, app):
    from server.app.overview import get_overview, parse_range

    today = date.today()
    _seed(app, [(today, "张三", "模板A", "伙伴甲", "success")])
    with app.app_context():
        start, end, label, preset = parse_range({"range": "today"})
        first = get_overview(start, end, label, preset)
        assert get_overview(start, end, label, preset) is first  # 15 秒内命中同一对象

    resp = client.post(
        "/api/v1/records",
        data={
            "employee_name": "王五",
            "template_name": "模板B",
            "partner_name": "伙伴乙",
            "author": "作者",
            "title": "新稿",
            "work_date": today.isoformat(),
        },
        headers=AUTH,
    )
    assert resp.status_code == 201

    with app.app_context():
        assert get_overview(start, end, label, preset) is not first  # 写入后缓存失效
        assert get_overview(start, end, label, preset)["kpi"]["calls"] == 2


# ---------- html 片段 ----------

def test_html_fragments_escape_user_data(client, clean_db, app):
    today = date.today()
    _seed(app, [(today, "张三", "<script>alert(1)</script>", '<b>伙伴&"名"</b>', "success")])
    html = _overview(client, range="today").get_json()["html"]
    # 表格里是伙伴名：不能原样出现 <b> 和 &
    assert "<b>伙伴" not in html["table"]
    assert "&lt;b&gt;伙伴" in html["table"]
    assert "&amp;" in html["table"]
    # 展开片段里是模板名：不能原样出现 <script>
    assert "<script>alert(1)</script>" not in html["details"][0]
    assert "&lt;script&gt;" in html["details"][0]


def test_details_aligned_with_partners(client, clean_db, app):
    today = date.today()
    _seed(
        app,
        [
            (today, "张三", "模板A", "伙伴甲", "success"),
            (today, "张三", "模板A", "伙伴甲", "success"),
            (today, "张三", "模板B", "伙伴乙", "success"),
        ],
    )
    data = _overview(client, range="today").get_json()
    assert len(data["html"]["details"]) == len(data["partners"]) == 2
    assert [p["name"] for p in data["partners"]] == ["伙伴甲", "伙伴乙"]  # 2 次 > 1 次
    assert "伙伴甲" in data["html"]["details"][0]
    assert "模板A" in data["html"]["details"][0]
    assert "detail-row" in data["html"]["details"][0]


def test_empty_range_has_empty_states(client, clean_db, app):
    data = _overview(client, range="today").get_json()
    assert data["kpi"]["calls"] == 0
    assert "本区间暂无调用记录" in data["html"]["trend"]
    assert "本区间暂无调用记录" in data["html"]["table"]
    assert data["html"]["details"] == []


# ---------- 页面与权限 ----------

def test_dashboard_page_server_rendered(admin_client, clean_db, app):
    today = date.today()
    _seed(app, [(today, "张三", "期刊模板A", "科学出版社", "success")])
    html = admin_client.get("/dashboard").get_data(as_text=True)
    assert "数据概览" in html
    assert "合作伙伴明细" in html
    assert "科学出版社" in html               # 首屏就有数据，不等 JS
    assert "模板数" in html                  # 明细表列头
    assert "状态口径" in html                # 口径写在页面底部，数字可信
    assert "最后备份" in html or "还没有数据库备份" in html  # 系统状态条还在
    assert "图片" in html
    assert "dashboard.js" in html
    # 模板明细在行展开的 payload 里（details 片段），不在首屏 HTML——
    # 首屏只出汇总，展开时才插入，页面才不会因为伙伴多而臃肿
    assert "期刊模板A" not in html


def test_dashboard_page_accepts_range_query(admin_client, clean_db, app):
    today = date.today()
    _seed(app, [(today, "张三", "模板A", "伙伴甲", "success")])
    html = admin_client.get("/dashboard?range=yesterday").get_data(as_text=True)
    assert "昨天" in html
    assert "伙伴甲" not in html  # 昨天没有记录


def test_dashboard_bad_range_falls_back_with_flash(admin_client, clean_db, app):
    html = admin_client.get("/dashboard?range=custom&start=2026-01-02&end=2026-01-01").get_data(as_text=True)
    assert "开始日期不能晚于结束日期" in html  # flash 提示
    assert "近 7 天" in html                    # 回退默认区间，不白屏


def test_viewer_session_can_read_overview(app, clean_db):
    """只读账号看概览：统计是读操作，viewer 放行"""
    from server.app.db import db
    from server.app.models import AdminUser

    with app.app_context():
        if not AdminUser.query.filter_by(username="viewer_overview").first():
            viewer = AdminUser(username="viewer_overview", role="viewer")
            viewer.set_password("viewer-pass-1")
            db.session.add(viewer)
            db.session.commit()

    c = app.test_client()
    assert c.post("/login", data={"username": "viewer_overview", "password": "viewer-pass-1"}).status_code == 302
    assert c.get("/api/v1/stats/overview").status_code == 200


# ---------- 伙伴表截断与显示全部 ----------

def test_limit_truncates_partner_table(client, clean_db, app):
    today = date.today()
    _seed(app, [(today, f"员工{i}", "模板A", f"伙伴{i:02d}", "success") for i in range(25)])
    data = _overview(client, range="today").get_json()
    assert len(data["partners"]) == 25                       # 数据是全的
    assert data["html"]["table"].count("partner-row") == 20   # 展示截到 20 行
    assert "显示全部 25 家" in data["html"]["table"]
    assert "还有 5 家未显示" in data["html"]["table"]
    assert "仅显示前 20 家" in data["html"]["note"]

    full = _overview(client, range="today", limit=0).get_json()
    assert full["html"]["table"].count("partner-row") == 25
    assert "显示全部" not in full["html"]["table"]
    assert len(full["html"]["details"]) == 25  # 展开数据不受截断影响


def test_limit_invalid_falls_back(client, clean_db, app):
    """limit 是展示偏好不是安全边界：手打非法值退回默认，不 500 不 400"""
    _seed(app, [(date.today(), "张三", "模板A", "伙伴甲", "success")])
    for bad in ("abc", "-5", "99999", "1.5"):
        assert _overview(client, range="today", limit=bad).status_code == 200


def test_top5_rows_are_jumpable(client, clean_db, app):
    """TOP5 横条要能点击/键盘定位到明细表对应行"""
    _seed(app, [(date.today(), "张三", "模板A", "伙伴甲", "success")])
    top = _overview(client, range="today").get_json()["html"]["top"]
    assert 'data-partner="伙伴甲"' in top
    assert 'class="bar-row top-row"' in top
    assert 'role="button"' in top
    assert 'tabindex="0"' in top


# ---------- 概览导出 CSV ----------

def test_overview_export_csv(client, clean_db, app):
    today = date.today()
    _seed(
        app,
        [
            (today, "张三", "期刊模板A", "科学出版社", "success"),
            (today, "李四", "期刊模板B", "科学出版社", "failed"),
        ],
    )
    resp = client.get("/api/v1/stats/overview/export", query_string={"range": "today"}, headers=AUTH)
    assert resp.status_code == 200
    assert "text/csv" in resp.content_type
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    body = resp.get_data(as_text=True)
    assert body.startswith("\ufeff")  # BOM：Excel 双击打开不乱码
    assert "数据概览导出" in body
    assert "合作伙伴明细" in body
    assert "各伙伴模板用量" in body
    assert "科学出版社,2,2,100.0" in body  # 伙伴行：2 个模板 2 次调用
    assert "期刊模板A,1,50.0" in body      # 模板行：每个模板用了几次
    assert "is_deleted=0 的全部记录（含失败）" in body  # 口径随导出走


def test_overview_export_requires_auth(client, clean_db):
    assert client.get("/api/v1/stats/overview/export").status_code == 401


def test_overview_export_guards_formula_injection(client, clean_db, app):
    """伙伴名/模板名以 = 开头时前置制表符，Excel 不当公式执行"""
    today = date.today()
    _seed(app, [(today, "张三", "=cmd|'/c calc'!A1", "=危险伙伴", "success")])
    body = client.get(
        "/api/v1/stats/overview/export", query_string={"range": "today"}, headers=AUTH
    ).get_data(as_text=True)
    assert "\t=危险伙伴" in body
    assert "\t=cmd" in body


def test_overview_export_writes_audit(client, clean_db, app):
    """导出不写库，但"谁把数据拷走了"必须查得到"""
    today = date.today()
    _seed(app, [(today, "张三", "模板A", "伙伴甲", "success")])
    assert client.get(
        "/api/v1/stats/overview/export", query_string={"range": "today"}, headers=AUTH
    ).status_code == 200
    with app.app_context():
        from server.app.models import AuditLog

        entry = AuditLog.query.filter_by(action="export_csv").order_by(AuditLog.id.desc()).first()
        assert entry is not None
        assert "概览" in (entry.detail or "")


def test_overview_export_rejects_bad_range(client, clean_db):
    resp = client.get(
        "/api/v1/stats/overview/export",
        query_string={"range": "custom", "start": "2026-01-02", "end": "2026-01-01"},
        headers=AUTH,
    )
    assert resp.status_code == 400


# ---------- 侧栏上次登录 ----------

def test_sidebar_shows_last_login(admin_client, clean_db):
    """admin_client 刚登录，last_login_at 已刷新，侧栏应显示时间"""
    import re

    html = admin_client.get("/dashboard").get_data(as_text=True)
    assert "上次登录" in html
    assert re.search(r"上次登录 \d{2}-\d{2} \d{2}:\d{2}", html)
