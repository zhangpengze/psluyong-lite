"""系统状态卡：组件行渲染、备份/推送/磁盘的分档口径。

覆盖这些「不测就不知道哪天坏」的点：
  - 五个组件行都在，文案随真实信号变（没有备份 → 红色"还没有数据库备份"）
  - 备份 36 小时分档：新鲜=绿、过期=琥珀、没有=红
  - 日报推送：已推送=绿、无记录=琥珀、失败=红、未配置通道=灰
  - 图片存储：阈值 80% 预警、超阈值异常，文案带阈值
  - 状态缓存跨用例要清，否则后一个用例看到前一个的环境
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import pytest

from server.app import status as status_mod


@pytest.fixture(autouse=True)
def _clear_caches():
    """概览缓存与状态缓存都要清：状态数据 30 秒 TTL，会串用例。

    状态缓存的初始键（at/ttl/data）是代码依赖的不变量，只能重置不能 clear。
    """
    from server.app.overview import _CACHE as overview_cache

    status_mod._CACHE.update(at=0.0, ttl=0.0, data=None)
    overview_cache.clear()
    yield
    status_mod._CACHE.update(at=0.0, ttl=0.0, data=None)
    overview_cache.clear()


@pytest.fixture()
def sys_dirs():
    """清空并返回测试用的备份 / 图片 / 日报目录（conftest 的 DATA_DIR 是临时目录）。

    图片目录必须清：test_web 的上传用例会往 IMAGES_DIR 落真实文件，
    不清的话"图片 N 张"的数会被别的文件污染（全量跑才能暴露，隔离跑不出来）。
    """
    from server.app.config import BACKUP_DIR, DATA_DIR, IMAGES_DIR

    for d in (BACKUP_DIR, IMAGES_DIR, DATA_DIR / "reports"):
        d.mkdir(parents=True, exist_ok=True)
    for f in BACKUP_DIR.glob("*.db.gz"):
        f.unlink()
    for f in IMAGES_DIR.rglob("*"):
        if f.is_file():
            f.unlink()
    for f in (DATA_DIR / "reports").glob("*.status.json"):
        f.unlink()
    return {"backup": BACKUP_DIR, "images": IMAGES_DIR, "reports": DATA_DIR / "reports"}


def _fake_backup(dirs, age_hours: float):
    path = dirs["backup"] / f"app-{datetime.now():%Y%m%d-%H%M%S}.db.gz"
    path.write_bytes(b"x")
    mtime = datetime.now() - timedelta(hours=age_hours)
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def _due_day():
    """仪表盘"日报"那一行查的是哪一天——按推送时点算，不是固定昨天。

    独立实现一遍（不 import 生产函数）：夹具要把文件放到"代码会去看的位置"，
    复用同一个函数会让"到底该查哪天"这条逻辑失去被验证的机会。
    """
    from server.app.config import Config

    now = datetime.now()
    due = (now.hour, now.minute) >= (Config.DAILY_REPORT_HOUR, Config.DAILY_REPORT_MINUTE)
    return now.date() if due else now.date() - timedelta(days=1)


def _push_status(dirs, pushed: bool, message: str = "", final: bool = False):
    day = _due_day()
    path = dirs["reports"] / f"{day:%Y-%m-%d}.status.json"
    path.write_text(
        json.dumps({"pushed": pushed, "message": message, "final": final}, ensure_ascii=False),
        encoding="utf-8",
    )


def _rows(client):
    with client.application.app_context():
        return status_mod.system_status()["rows"]


# ---------- 渲染 ----------

def test_all_five_component_rows_render(admin_client, clean_db, sys_dirs):
    html = admin_client.get("/dashboard").get_data(as_text=True)
    assert "系统状态" in html
    assert "操作日志" in html
    for label in ("后台服务", "数据库", "数据库备份", "日报推送", "图片存储"):
        assert label in html
    assert "图片对账" not in html  # 对账功能已按需求移除
    assert html.count('class="sys-row"') == 5


def test_no_backup_row_is_red(admin_client, clean_db, sys_dirs):
    rows = _rows(admin_client)
    assert rows["backup"]["status"] == "bad"
    assert "还没有数据库备份" in rows["backup"]["detail"]
    html = admin_client.get("/dashboard").get_data(as_text=True)
    assert "sys-dot bad" in html


def test_service_row_shows_uptime(admin_client, clean_db, sys_dirs):
    rows = _rows(admin_client)
    assert rows["service"]["status"] == "ok"
    assert "已运行" in rows["service"]["detail"] or "刚启动" in rows["service"]["detail"]


def test_database_row_shows_records(admin_client, clean_db, app, sys_dirs):
    from server.app.db import db
    from server.app.models import Record

    with app.app_context():
        db.session.add(
            Record(
                work_date=date.today(),
                employee_name="张三",
                template_name="模板A",
                partner_name="伙伴甲",
                author="作者",
                title="标题",
                status="success",
            )
        )
        db.session.commit()
    rows = _rows(admin_client)
    assert "1 条" in rows["database"]["detail"]


# ---------- 备份分档 ----------

def test_fresh_backup_is_ok(admin_client, clean_db, sys_dirs):
    _fake_backup(sys_dirs, age_hours=2)
    rows = _rows(admin_client)
    assert rows["backup"]["status"] == "ok"
    assert "最后备份 2 小时前" in rows["backup"]["detail"]


def test_stale_backup_is_warn(admin_client, clean_db, sys_dirs):
    _fake_backup(sys_dirs, age_hours=48)
    rows = _rows(admin_client)
    assert rows["backup"]["status"] == "warn"
    assert "最后备份 2 天前" in rows["backup"]["detail"]


# ---------- 日报推送分档 ----------

def test_report_off_when_webhook_missing(admin_client, clean_db, sys_dirs, monkeypatch):
    """没配 NAPCAT_WEBHOOK：灰色"未配置"，不算故障。

    注意：load_dotenv 会把仓库根目录真实 .env 的值读回来，测试环境默认
    「已配置」，所以这里必须显式置空才能测到未配置分支。
    """
    from server.app.config import Config

    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "")
    rows = _rows(admin_client)
    assert rows["report"]["status"] == "off"
    assert "未配置" in rows["report"]["detail"]


def test_report_pushed_is_ok(admin_client, clean_db, sys_dirs, monkeypatch):
    from server.app.config import Config

    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "http://napcat.fake/webhook")
    _push_status(sys_dirs, pushed=True)
    rows = _rows(admin_client)
    assert rows["report"]["status"] == "ok"
    assert "已推送" in rows["report"]["detail"]


def test_report_missing_status_is_warn(admin_client, clean_db, sys_dirs, monkeypatch):
    from server.app.config import Config

    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "http://napcat.fake/webhook")
    rows = _rows(admin_client)
    assert rows["report"]["status"] == "warn"
    assert "没有推送记录" in rows["report"]["detail"]


def test_report_failed_is_bad_with_reason(admin_client, clean_db, sys_dirs, monkeypatch):
    from server.app.config import Config

    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "http://napcat.fake/webhook")
    _push_status(sys_dirs, pushed=False, message="连接超时")
    rows = _rows(admin_client)
    assert rows["report"]["status"] == "bad"
    assert "推送失败" in rows["report"]["detail"]
    assert "连接超时" in rows["report"]["detail"]


def test_report_final_is_marked(admin_client, clean_db, sys_dirs, monkeypatch):
    """出过终版的日报要能看出来（快报 vs 终版，内容不一样，别让人以为推重了）"""
    from server.app.config import Config

    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "http://napcat.fake/webhook")
    _push_status(sys_dirs, pushed=True, final=True)
    rows = _rows(admin_client)
    assert rows["report"]["status"] == "ok"
    assert "终版" in rows["report"]["detail"]


@pytest.mark.parametrize(
    "hour,minute,days_back",
    [(17, 59, 1), (18, 0, 0), (18, 30, 0), (9, 0, 1)],
)
def test_report_row_checks_the_day_that_should_be_pushed(monkeypatch, hour, minute, days_back):
    """过了推送时点就看今天，没过就看昨天——旧实现固定看昨天，18:00 没推成
    要等 24 小时才变黄点"""
    from server.app.config import Config

    monkeypatch.setattr(Config, "DAILY_REPORT_HOUR", 18)
    monkeypatch.setattr(Config, "DAILY_REPORT_MINUTE", 0)
    now = datetime(2026, 10, 24, hour, minute)
    assert status_mod._due_day(now) == date(2026, 10, 24 - days_back)


# ---------- 图片存储分档 ----------

def test_images_under_threshold_is_ok(admin_client, clean_db, sys_dirs, monkeypatch):
    from server.app.config import Config

    (sys_dirs["images"] / "a.jpg").write_bytes(b"x" * 1024)
    monkeypatch.setattr(Config, "DISK_ALERT_GB", 20)
    rows = _rows(admin_client)
    assert rows["images"]["status"] == "ok"
    assert "阈值 20 GB" in rows["images"]["detail"]
    assert "图片 1 张" in rows["images"]["detail"]


def test_images_near_threshold_is_warn(admin_client, clean_db, sys_dirs, monkeypatch):
    from server.app.config import Config

    size = 1024**3  # 1 GB
    (sys_dirs["images"] / "big.jpg").write_bytes(b"x" * size)
    # 阈值 1.1 GB：占用 91%，落在 80%~100% 预警区
    monkeypatch.setattr(Config, "DISK_ALERT_GB", 1.1)
    rows = _rows(admin_client)
    assert rows["images"]["status"] == "warn"


def test_images_over_threshold_is_bad(admin_client, clean_db, sys_dirs, monkeypatch):
    from server.app.config import Config

    (sys_dirs["images"] / "big.jpg").write_bytes(b"x" * 1024)
    # 阈值约 107 字节，稳稳小于文件的 1024 字节
    # （曾写成 0.000001 GB≈1073 字节，比文件还大，永远测不到异常档）
    monkeypatch.setattr(Config, "DISK_ALERT_GB", 0.0000001)
    rows = _rows(admin_client)
    assert rows["images"]["status"] == "bad"


# ---------- 接口形状 ----------

def test_api_system_shape(client, clean_db, sys_dirs):
    from tests.conftest import API_KEY

    data = client.get(
        "/api/v1/stats/overview", query_string={"range": "today"}, headers={"X-API-Key": API_KEY}
    ).get_json()
    rows = data["system"]["rows"]
    assert set(rows) == {"service", "database", "backup", "report", "images"}
    for row in rows.values():
        assert row["status"] in ("ok", "warn", "bad", "off")
        assert row["detail"]


# ---------- 单行取数失败不能拖垮整张卡 ----------

@pytest.mark.parametrize(
    "row_name",
    ["database", "backup", "report", "images", "service"],
)
def test_one_broken_row_degrades_only_itself(client, clean_db, sys_dirs, monkeypatch, row_name):
    """任一行取数抛错 → 该行变红并带原因，其余四行照常。

    回归的是 _database_row 里 `Record.query.count()` 没有 try/except：
    WAL 高峰期一次锁冲突就能让整张卡和 /api/v1/stats/overview 一起 500，
    而"系统好不好"恰恰是故障时最该打得开的那一页。
    """

    def boom(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(status_mod, f"_{row_name}_row", boom)
    rows = _rows(client)

    assert set(rows) == {"service", "database", "backup", "report", "images"}
    broken = rows[row_name]
    assert broken["status"] == "bad"
    assert "取数失败" in broken["detail"] and "RuntimeError" in broken["detail"]
    assert "database is locked" in broken["error"]
    for other in set(rows) - {row_name}:
        assert rows[other]["status"] != "bad" or "取数失败" not in rows[other]["detail"]


def test_broken_row_does_not_500_the_overview_api(client, clean_db, sys_dirs, monkeypatch):
    from tests.conftest import API_KEY

    monkeypatch.setattr(
        status_mod, "_database_row", lambda: (_ for _ in ()).throw(RuntimeError("locked"))
    )
    resp = client.get(
        "/api/v1/stats/overview", query_string={"range": "today"}, headers={"X-API-Key": API_KEY}
    )
    assert resp.status_code == 200, "单行取数失败不许把概览接口打成 500"
    assert resp.get_json()["system"]["rows"]["database"]["status"] == "bad"


def test_broken_row_still_renders_dashboard(admin_client, clean_db, sys_dirs, monkeypatch):
    monkeypatch.setattr(
        status_mod, "_images_row", lambda: (_ for _ in ()).throw(OSError("stale handle"))
    )
    resp = admin_client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "取数失败" in html, "降级原因要露在页面上，否则运维看不出是磁盘还是数据库"


def test_safe_row_passes_through_healthy_builder():
    assert status_mod._safe_row("x", lambda: {"status": "ok", "detail": "fine"}) == {
        "status": "ok",
        "detail": "fine",
    }
