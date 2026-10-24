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


def test_same_paper_by_two_employees_allowed(client, clean_db, sample_image):
    """两个员工同一天做同一篇稿子不该互相挡：去重键含 employee_name。

    旧键 (work_date, author, title) 下，第二个人会被判"重复提交"，
    白跑一次 PS 还拿不到记录。
    """
    first = _submit(client, sample_image)
    assert first.status_code == 201

    second = _submit(client, sample_image, employee_name="李四")
    assert second.status_code == 201, second.get_data(as_text=True)
    assert second.get_json()["data"]["employee_name"] == "李四"

    # 同一个人重复提交仍然要拦
    dup = _submit(client, sample_image)
    assert dup.status_code == 409 and dup.get_json()["duplicate"] is True


def test_same_employee_two_templates_allowed(client, clean_db, sample_image):
    """同一员工同一天同一篇稿出两个期刊模板是合理业务，不能拦"""
    assert _submit(client, sample_image, template_name="期刊A模板").status_code == 201
    assert _submit(client, sample_image, template_name="期刊B模板").status_code == 201


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


def test_viewer_cannot_write(app, clean_db, sample_image, admin_client, admin_csrf):
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


def test_delete_requires_csrf_token(admin_client, admin_csrf, clean_db, sample_image):
    """会话写操作必须带 CSRF token，防止跨站冒用管理员会话"""
    record_id = _submit(admin_client, sample_image).get_json()["data"]["id"]

    # 不带 token：403，记录还在
    assert admin_client.delete(f"/api/v1/records/{record_id}").status_code == 403
    assert admin_client.get("/api/v1/records", headers=AUTH).get_json()["total"] == 1

    # 带对 token：200
    assert admin_client.delete(f"/api/v1/records/{record_id}", headers=admin_csrf).status_code == 200

    # 带错 token：403
    bad = _submit(admin_client, sample_image, author="王五", title="另一篇").get_json()["data"]["id"]
    assert admin_client.delete(f"/api/v1/records/{bad}", headers={"X-CSRF-Token": "wrong"}).status_code == 403


def test_soft_delete_hides_from_list(admin_client, admin_csrf, clean_db, sample_image):
    record_id = _submit(admin_client, sample_image).get_json()["data"]["id"]
    assert admin_client.delete(f"/api/v1/records/{record_id}", headers=admin_csrf).status_code == 200

    listing = admin_client.get("/api/v1/records", headers=AUTH).get_json()
    assert listing["total"] == 0


def test_recycle_bin_restore(admin_client, admin_csrf, clean_db, sample_image):
    """删除 → 回收站可见 → 恢复 → 重回列表；重复恢复 404"""
    record_id = _submit(admin_client, sample_image).get_json()["data"]["id"]
    assert admin_client.delete(f"/api/v1/records/{record_id}", headers=admin_csrf).status_code == 200

    # 软删除记录仍可取详情与图片（回收站预览用）
    assert admin_client.get(f"/api/v1/records/{record_id}").get_json()["ok"] is True

    # 记录页渲染回收站
    page = admin_client.get("/records").get_data(as_text=True)
    assert "回收站" in page

    # 恢复
    resp = admin_client.post(f"/api/v1/records/{record_id}/restore", headers=admin_csrf)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert admin_client.get("/api/v1/records", headers=AUTH).get_json()["total"] == 1

    # 重复恢复：404
    assert admin_client.post(f"/api/v1/records/{record_id}/restore", headers=admin_csrf).status_code == 404


def test_restore_rejects_duplicate(admin_client, admin_csrf, clean_db, sample_image):
    """恢复时若已存在同一天+同作者+同标题的未删除记录，409 不恢复"""
    first = _submit(admin_client, sample_image).get_json()["data"]["id"]
    assert admin_client.delete(f"/api/v1/records/{first}", headers=admin_csrf).status_code == 200
    # 同一天同作者同标题再提交一条（走并发冲突之外的正常路径：先删后提）
    second = _submit(admin_client, sample_image).get_json()["data"]["id"]
    assert second != first

    resp = admin_client.post(f"/api/v1/records/{first}/restore", headers=admin_csrf)
    assert resp.status_code == 409
    assert "无法恢复" in resp.get_json()["error"]


def test_report_run_requires_csrf(admin_client, admin_csrf):
    """手动跑日报同样受 CSRF 保护"""
    assert admin_client.post("/api/v1/reports/run", json={}).status_code == 403
    resp = admin_client.post("/api/v1/reports/run", headers=admin_csrf, json={"push": False})
    assert resp.status_code == 200, resp.get_data(as_text=True)


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
    assert "按员工" not in content  # 按需求已移除该段落
    assert "· 示例合作伙伴：1 条" in content
    assert "- 录用通知模板：1 条" in content
    assert "录用通知模板" in content


def _wait_alert(calls, timeout_s=3.0):
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if calls:
            return True
        time.sleep(0.05)
    return False


def test_failed_record_triggers_instant_alert(app, clean_db, sample_image, client, monkeypatch):
    """失败记录提交后立刻推告警（异步线程，不阻塞接口）"""
    calls = []
    monkeypatch.setattr("bot.report.push_alert", lambda content: calls.append(content) or (True, "ok"))

    resp = _submit(client, sample_image, status="failed", error_message="Photoshop 未响应")
    assert resp.status_code == 201

    assert _wait_alert(calls), "失败告警没有被触发"
    assert "Photoshop 未响应" in calls[0]
    assert "张三" in calls[0]  # 员工名


def test_success_record_does_not_alert(app, clean_db, sample_image, client, monkeypatch):
    """成功记录不触发告警"""
    calls = []
    monkeypatch.setattr("bot.report.push_alert", lambda content: calls.append(content) or (True, "ok"))

    resp = _submit(client, sample_image)
    assert resp.status_code == 201

    assert not _wait_alert(calls, timeout_s=0.5)


def test_alert_disabled_by_config(app, clean_db, sample_image, client, monkeypatch):
    """ALERT_ON_FAILURE=0 时连线程都不起"""
    from server.app.config import Config

    monkeypatch.setattr(Config, "ALERT_ON_FAILURE", False)
    calls = []
    monkeypatch.setattr("bot.report.push_alert", lambda content: calls.append(content) or (True, "ok"))

    resp = _submit(client, sample_image, status="failed")
    assert resp.status_code == 201

    assert not _wait_alert(calls, timeout_s=0.5)


def test_localtime_filter_converts_utc_to_config_tz(app):
    """回归：Windows 上 TZ 环境变量会让 astimezone() 退化成 UTC，
    页面时间曾整体偏 8 小时。现在显式用 zoneinfo。"""
    from datetime import datetime

    with app.app_context():
        fmt = app.jinja_env.filters["localtime"]
        assert fmt(datetime(2026, 10, 20, 11, 23)) == "10-20 19:23"  # UTC 11:23 -> +08
        assert fmt(None) == "-"


def test_failed_then_retry_backfills_same_record(client, clean_db, sample_image):
    """失败后重试同一篇必须放行，并回填同一条记录。

    真实场景：PS 没开 → 提交一条 failed → 员工修好 PS 再点一次生成。
    旧逻辑下这次重试会被自己刚才那条失败记录判成 409「请勿重复提交」，
    员工只能改标题造假，或等管理员去删记录。
    """
    failed = _submit(client, sample_image, status="failed", error_message="PS 未运行")
    assert failed.status_code == 201
    failed_id = failed.get_json()["data"]["id"]

    retry = _submit(client, sample_image)
    assert retry.status_code == 201, retry.get_data(as_text=True)
    data = retry.get_json()["data"]
    assert data["status"] == "success"
    assert data["id"] == failed_id, "应回填原失败记录，而不是新增一条"

    # 上一次的失败原因不能丢，要并进 warnings 供排障
    assert any("上次失败" in w and "PS 未运行" in w for w in data["warnings"])

    # 统计不能被历史失败拉低：1 条记录、0 失败
    listing = client.get("/api/v1/records", headers=AUTH).get_json()
    assert listing["total"] == 1
    assert listing["items"][0]["status"] == "success"


def test_repeated_failure_keeps_one_row(client, clean_db, sample_image):
    """连续失败三次也只留一条记录，最新原因覆盖旧的"""
    for i in range(3):
        resp = _submit(client, sample_image, status="failed", error_message=f"第{i + 1}次失败")
        assert resp.status_code == 201

    listing = client.get("/api/v1/records", headers=AUTH).get_json()
    assert listing["total"] == 1
    assert listing["items"][0]["error_message"] == "第3次失败"


def test_success_then_duplicate_still_rejected(client, clean_db, sample_image):
    """成功记录之间仍然要判重——修复失败重试不能把真正的重复录入也放过去"""
    assert _submit(client, sample_image).status_code == 201
    dup = _submit(client, sample_image, status="failed", error_message="重试")
    assert dup.status_code == 409 and dup.get_json()["duplicate"] is True


def test_export_reports_truncation(client, clean_db, sample_image, monkeypatch):
    """导出被行数上限截断时要明说，不能让人拿半张表去对账"""
    monkeypatch.setattr("server.app.routes.records.EXPORT_MAX_ROWS", 1)
    _submit(client, sample_image)
    _submit(client, sample_image, author="王五", title="另一篇稿件")

    resp = client.get("/api/v1/records/export", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["X-Export-Truncated"] == "1"
    assert resp.headers["X-Export-Total"] == "2"
    assert resp.headers["X-Export-Limit"] == "1"


def test_export_without_truncation_has_no_header(client, clean_db, sample_image):
    _submit(client, sample_image)
    resp = client.get("/api/v1/records/export", headers=AUTH)
    assert resp.status_code == 200
    assert "X-Export-Truncated" not in resp.headers
