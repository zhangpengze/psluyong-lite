"""CSV 导出与记录页增强（快捷日期、详情抽屉数据、URL 干净度）"""

from __future__ import annotations

import csv
import io

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
        "image": (make_image(), "result.png"),
    }
    payload.update(overrides)
    return client.post("/api/v1/records", data=payload, headers=AUTH, content_type="multipart/form-data")


def _parse_csv(resp):
    """带 BOM 的 UTF-8 CSV → (表头, 行列表)"""
    raw = resp.get_data(as_text=True)
    assert raw.startswith("\ufeff"), "CSV 缺 UTF-8 BOM，Excel 打开会乱码"
    rows = list(csv.reader(io.StringIO(raw.lstrip("\ufeff"))))
    return rows[0], rows[1:]


def test_export_csv_with_session(admin_client, admin_csrf, clean_db, sample_image):
    _submit(admin_client, sample_image)
    _submit(admin_client, sample_image, author="王五", title="另一篇稿件", status="failed", error_message="PS 未运行")

    resp = admin_client.get("/api/v1/records/export")
    assert resp.status_code == 200
    assert "text/csv" in resp.content_type
    assert "attachment" in resp.headers["Content-Disposition"]
    assert ".csv" in resp.headers["Content-Disposition"]

    header, rows = _parse_csv(resp)
    assert header[0] == "工作日期" and "作者" in header and "提交机器" in header
    assert len(rows) == 2

    by_author = {r[5]: r for r in rows}
    assert by_author["王五"][8] == "失败"  # 状态列
    assert by_author["王五"][9] == "PS 未运行"  # 错误信息列
    assert by_author["李四"][7] == "8.2"  # 耗时列：毫秒转秒
    assert by_author["李四"][8] == "成功"


def test_export_csv_with_api_key(client, clean_db, sample_image):
    _submit(client, sample_image)
    resp = client.get("/api/v1/records/export", headers=AUTH)
    assert resp.status_code == 200
    _header, rows = _parse_csv(resp)
    assert len(rows) == 1


def test_export_csv_requires_auth(client, clean_db, sample_image):
    _submit(client, sample_image)
    assert client.get("/api/v1/records/export").status_code == 401


def test_export_csv_respects_filters(admin_client, clean_db, sample_image):
    _submit(admin_client, sample_image, author="李四", title="甲")
    _submit(admin_client, sample_image, author="王五", title="乙", employee_name="李强")

    resp = admin_client.get("/api/v1/records/export?employee=" + "%E5%BC%A0%E4%B8%89")
    assert resp.status_code == 200
    _header, rows = _parse_csv(resp)
    assert len(rows) == 1 and rows[0][5] == "李四"


def test_export_csv_excludes_deleted(admin_client, admin_csrf, clean_db, sample_image):
    first = _submit(admin_client, sample_image).get_json()["data"]["id"]
    _submit(admin_client, sample_image, author="王五", title="另一篇")
    assert admin_client.delete(f"/api/v1/records/{first}", headers=admin_csrf).status_code == 200

    resp = admin_client.get("/api/v1/records/export")
    _header, rows = _parse_csv(resp)
    assert len(rows) == 1 and rows[0][5] == "王五"


def test_export_csv_blocks_formula_injection(admin_client, clean_db, sample_image):
    """=1+1 这类内容进 Excel 会被当公式执行，导出必须前置制表符"""
    _submit(admin_client, sample_image, author="=cmd|' /c calc'!A1", title="公式注入测试")

    resp = admin_client.get("/api/v1/records/export")
    _header, rows = _parse_csv(resp)
    assert rows[0][5].startswith("\t")
    assert rows[0][5].lstrip("\t") == "=cmd|' /c calc'!A1"


def test_records_page_has_quick_ranges_and_export(admin_client, clean_db, sample_image):
    _submit(admin_client, sample_image)
    page = admin_client.get("/records").get_data(as_text=True)

    # 快捷日期 chips
    for label in ("今天", "近 7 天", "本月", "全部"):
        assert label in page, f"缺少快捷日期 {label}"
    # 激活态：默认就是近 7 天
    assert "chip active" in page

    # 导出入口
    assert "/api/v1/records/export?" in page

    # URL 干净度：不再出现 employee=&partner= 这类空参数
    assert "employee=&" not in page and "partner=&" not in page and "status=&" not in page


def test_records_page_quick_range_links_keep_filters(admin_client, clean_db, sample_image):
    _submit(admin_client, sample_image, employee_name="张三")
    page = admin_client.get("/records?employee=" + "%E5%BC%A0%E4%B8%89").get_data(as_text=True)
    # 点快捷日期不应丢掉员工筛选
    assert "employee=%E5%BC%A0%E4%B8%89" in page


def test_record_detail_includes_warnings(admin_client, clean_db, sample_image):
    """详情抽屉的数据源：GET 单条要带 warnings 与审计字段"""
    record_id = _submit(
        admin_client,
        sample_image,
        warnings='["图层「标题」字体不可用"]',
        client_machine="WIN-TEST",
        ps_version="26.0",
    ).get_json()["data"]["id"]

    data = admin_client.get(f"/api/v1/records/{record_id}").get_json()["data"]
    assert data["warnings"] == ["图层「标题」字体不可用"]
    assert data["client_machine"] == "WIN-TEST"
    assert data["ps_version"] == "26.0"
    assert data["template_id"] is None or isinstance(data["template_id"], str)
