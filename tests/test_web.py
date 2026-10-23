"""后台页面与最近补的稳健性改动

覆盖这些「不测就不知道哪天坏」的点：
  - 分页越界要夹回最后一页（否则大 offset 白扫表，页头还显示 999999/2）
  - 只读账号在页面上看不到删除按钮
  - 缩略图要带长缓存头，翻页不用重复下载
  - 客户端时钟错乱提交的未来日期要被拒
  - 登录失败次数超限要限流
  - 删除要落审计流水，后台能查到
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from tests.conftest import API_KEY

AUTH = {"X-API-Key": API_KEY}


def _seed(app, count: int = 25, **overrides) -> None:
    from server.app.db import db
    from server.app.models import Record

    with app.app_context():
        for i in range(count):
            payload = {
                "work_date": date.today(),
                "employee_name": "张三",
                "template_name": "录制模板",
                "partner_name": "示例伙伴",
                "author": f"作者{i}",
                "title": f"标题{i}",
                "status": "success",
                "processing_ms": 5000,
            }
            payload.update(overrides)
            db.session.add(Record(**payload))
        db.session.commit()


def test_records_page_clamps_out_of_range_page(admin_client, clean_db, app):
    """?page=999 应夹到最后一页，而不是渲染空表 + 999/2"""
    _seed(app, 30)
    html = admin_client.get("/records?page=999").get_data(as_text=True)
    assert "2 / 2" in html, "越界页码没有被夹回最后一页"
    assert "999" not in html


def test_api_list_clamps_out_of_range_page(client, clean_db, app):
    _seed(app, 30)
    data = client.get("/api/v1/records?page=999", headers=AUTH).get_json()
    assert data["page"] == 2 and data["pages"] == 2


def test_viewer_sees_no_delete_button(app, clean_db):
    """只读账号进记录页：能看，但不该出现删除入口"""
    from server.app.db import db
    from server.app.models import AdminUser

    _seed(app, 2)
    with app.app_context():
        if not AdminUser.query.filter_by(username="viewer_page").first():
            viewer = AdminUser(username="viewer_page", role="viewer")
            viewer.set_password("viewer-pass-1")
            db.session.add(viewer)
            db.session.commit()

    c = app.test_client()
    assert c.post("/login", data={"username": "viewer_page", "password": "viewer-pass-1"}).status_code == 302
    html = c.get("/records").get_data(as_text=True)
    assert "data-delete=" not in html
    assert "作者0" in html


def test_thumb_has_cache_header(client, clean_db, sample_image):
    resp = client.post(
        "/api/v1/records",
        data={
            "employee_name": "张三",
            "template_name": "录制模板",
            "author": "李四",
            "title": "带图的稿子",
            "image": (sample_image(), "a.png"),
        },
        headers=AUTH,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    record_id = resp.get_json()["data"]["id"]

    thumb = client.get(f"/api/v1/records/{record_id}/image?thumb=1", headers=AUTH)
    assert thumb.status_code == 200
    assert "max-age=604800" in thumb.headers.get("Cache-Control", "")

    # 原图不该被长缓存（内容可能被替换，缓存久了会看到旧图）
    full = client.get(f"/api/v1/records/{record_id}/image", headers=AUTH)
    assert "604800" not in full.headers.get("Cache-Control", "")


def test_future_work_date_rejected(client, clean_db, sample_image):
    future = (date.today() + timedelta(days=30)).isoformat()
    resp = client.post(
        "/api/v1/records",
        data={
            "employee_name": "张三",
            "template_name": "录制模板",
            "author": "李四",
            "title": "未来日期的稿子",
            "work_date": future,
        },
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert "日期" in resp.get_json()["error"]


def test_login_rate_limited(app):
    """同一来源连续失败 5 次后应返回 429（用独立 IP，别污染其他用例）"""
    from server.app import ratelimit

    ip = {"REMOTE_ADDR": "10.99.99.99"}
    ratelimit.reset(f"login:{ip['REMOTE_ADDR']}")

    c = app.test_client()
    for _ in range(5):
        assert c.post("/login", data={"username": "admin", "password": "wrong"}, environ_base=ip).status_code == 401

    resp = c.post("/login", data={"username": "admin", "password": "wrong"}, environ_base=ip)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert "尝试次数过多" in resp.get_data(as_text=True)

    ratelimit.reset(f"login:{ip['REMOTE_ADDR']}")


def test_delete_writes_audit_log(admin_client, admin_csrf, clean_db, app):
    _seed(app, 1)
    with app.app_context():
        from server.app.models import Record

        record_id = Record.query.first().id

    assert admin_client.delete(f"/api/v1/records/{record_id}", headers=admin_csrf).status_code == 200

    page = admin_client.get("/audit").get_data(as_text=True)
    assert "删除记录" in page
    assert f"record:{record_id}" in page


def test_audit_page_filters_by_action(admin_client, clean_db):
    resp = admin_client.get("/audit?action=delete_record")
    assert resp.status_code == 200
    assert "全部动作" in resp.get_data(as_text=True)


def test_dashboard_shows_system_status(admin_client, clean_db):
    html = admin_client.get("/dashboard").get_data(as_text=True)
    assert "最后备份" in html or "还没有数据库备份" in html
    assert "图片" in html


def test_security_headers_present(admin_client):
    resp = admin_client.get("/dashboard")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert resp.headers["Referrer-Policy"] == "same-origin"


def test_csrf_meta_and_asset_version(admin_client):
    html = admin_client.get("/dashboard").get_data(as_text=True)
    assert re.search(r'<meta name="csrf-token" content="[^"]+"', html)
    assert re.search(r"app\.css\?v=\d+", html), "静态资源缺少版本号，改样式后用户会看到旧缓存"
