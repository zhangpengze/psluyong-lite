"""端到端冒烟：录用记录 → 落库 → 后台可见 → 统计 → 日报"""

from __future__ import annotations

from datetime import date
from io import BytesIO

from tests.conftest import API_KEY

AUTH = {"X-API-Key": API_KEY}


def _submit(client, make_image, **overrides):
    payload = {
        "employee_name": "张三",
        "template_name": "录用通知模板",
        "partner_name": "示例合作伙伴",
        "author": "李四",
        "title": "基于深度学习的图像分割研究",
        "client_machine": "DESKTOP-001",
        "ps_version": "25.0",
        "status": "success",
        "processing_ms": "8200",
        "warnings": '["图层「标题」字体不可用，请检查替代字体"]',
        "image": (make_image(), "result.png"),
    }
    payload.update(overrides)
    return client.post("/api/v1/records", data=payload, headers=AUTH, content_type="multipart/form-data")


def test_health(client):
    assert client.get("/health").status_code == 200
    resp = client.get("/health/ready")
    assert resp.status_code == 200 and resp.get_json()["db"] == "ok"


def test_login_required_redirects(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_wrong_password_rejected(client):
    resp = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


def test_submit_requires_api_key(client):
    resp = client.post("/api/v1/records", data={"employee_name": "张三"})
    assert resp.status_code == 401


def test_submit_record_with_image(client, clean_db, sample_image):
    resp = _submit(client, sample_image)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    data = resp.get_json()["data"]

    assert data["employee_name"] == "张三"
    assert data["author"] == "李四"
    assert data["has_image"] is True
    assert data["warnings"] == ["图层「标题」字体不可用，请检查替代字体"]

    # 图片能取回来，缩略图也生成了
    img = client.get(f"/api/v1/records/{data['id']}/image", headers=AUTH)
    assert img.status_code == 200 and len(img.data) > 0
    thumb = client.get(f"/api/v1/records/{data['id']}/image?thumb=1", headers=AUTH)
    assert thumb.status_code == 200 and len(thumb.data) > 0


def test_duplicate_record_rejected(client, clean_db, sample_image):
    assert _submit(client, sample_image).status_code == 201
    second = _submit(client, sample_image)
    assert second.status_code == 409
    assert second.get_json()["duplicate"] is True


def test_missing_author_or_title(client, clean_db, sample_image):
    resp = _submit(client, sample_image, author="")
    assert resp.status_code == 400


def test_list_and_filter(client, clean_db, sample_image):
    _submit(client, sample_image)
    _submit(client, sample_image, author="王五", title="另一篇稿件", partner_name="另一个伙伴")

    all_rows = client.get("/api/v1/records", headers=AUTH).get_json()
    assert all_rows["total"] == 2

    filtered = client.get("/api/v1/records?partner=另一个伙伴", headers=AUTH).get_json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["author"] == "王五"

    searched = client.get("/api/v1/records?q=深度", headers=AUTH).get_json()
    assert searched["total"] == 1


def test_search_escapes_like_wildcards(client, clean_db, sample_image):
    """搜 % 和 _ 必须按字面匹配，不能当通配符命中全表（旧 bug）"""
    _submit(client, sample_image, author="王五", title="占比50%的样本")
    _submit(client, sample_image, author="赵六", title="带下划线的_标题")
    _submit(client, sample_image, author="孙七", title="普通标题A")
    _submit(client, sample_image, author="周八", title="普通标题B")

    # 只命中字面含 % 的那条，而不是全部 4 条
    assert client.get("/api/v1/records?q=%25", headers=AUTH).get_json()["total"] == 1
    assert client.get("/api/v1/records?q=50%25", headers=AUTH).get_json()["total"] == 1
    # 字面下划线同理
    assert client.get("/api/v1/records?q=的_", headers=AUTH).get_json()["total"] == 1
    # 普通关键词不受影响
    assert client.get("/api/v1/records?q=普通标题", headers=AUTH).get_json()["total"] == 2


def test_stats_daily(client, clean_db, sample_image):
    _submit(client, sample_image)
    _submit(client, sample_image, author="王五", title="另一篇稿件")
    _submit(client, sample_image, author="赵六", title="失败的", status="failed", error_message="PS 未运行")

    stats = client.get(f"/api/v1/stats/daily?date={date.today()}", headers=AUTH).get_json()
    assert stats["total"] == 3
    assert stats["failed"] == 1
    assert stats["success"] == 2
    assert stats["by_employee"][0] == {"name": "张三", "count": 3}


def test_viewer_cannot_write(app, clean_db, sample_image, admin_client):
    """只读账号不能删记录、不能手动跑日报"""
    from server.app.db import db
    from server.app.models import AdminUser

    record_id = _submit(admin_client, sample_image).get_json()["data"]["id"]

    with app.app_context():
        viewer = AdminUser(username="viewer1", role="viewer")
        viewer.set_password("viewer-pass")
        db.session.add(viewer)
        db.session.commit()

    c = app.test_client()
    assert c.post("/login", data={"username": "viewer1", "password": "viewer-pass"}).status_code == 302

    assert c.delete(f"/api/v1/records/{record_id}").status_code == 403
    assert c.post("/api/v1/reports/run", json={}).status_code == 403

    # 读还是可以的
    assert c.get("/api/v1/records").status_code == 200


def test_soft_delete_hides_from_list(admin_client, clean_db, sample_image):
    record_id = _submit(admin_client, sample_image).get_json()["data"]["id"]
    assert admin_client.delete(f"/api/v1/records/{record_id}").status_code == 200

    listing = admin_client.get("/api/v1/records", headers=AUTH).get_json()
    assert listing["total"] == 0


def test_pages_render(admin_client, clean_db, sample_image):
    _submit(admin_client, sample_image)
    for path in ("/dashboard", "/records", "/reports"):
        resp = admin_client.get(path)
        assert resp.status_code == 200, f"{path} 渲染失败"
        assert "录用" in resp.get_data(as_text=True)


def test_removed_pages_are_gone(admin_client):
    """模板与字典已移交员工客户端，后台不该再留入口"""
    for path in ("/templates", "/dicts"):
        assert admin_client.get(path).status_code == 404

    nav = admin_client.get("/dashboard").get_data(as_text=True)
    assert "模板管理" not in nav and "字典维护" not in nav


def test_daily_report_contains_numbers(app, clean_db, sample_image, client):
    _submit(client, sample_image)
    from bot.report import build_daily_report

    content = build_daily_report(app, date.today())
    assert "总录用：1 条" in content
    assert "张三：1 条" in content
    assert "录用通知模板" in content


def test_localtime_filter_converts_utc_to_config_tz(app):
    """回归：Windows 上 TZ 环境变量会让 astimezone() 退化成 UTC，
    页面时间曾整体偏 8 小时。现在显式用 zoneinfo。"""
    from datetime import datetime

    with app.app_context():
        fmt = app.jinja_env.filters["localtime"]
        assert fmt(datetime(2026, 10, 20, 11, 23)) == "10-20 19:23"  # UTC 11:23 -> +08
        assert fmt(None) == "-"
