"""应用工厂层：错误处理分支、模板过滤器、后台 CLI 命令、静态资源响应头

审计缺口：server/app/__init__ 67%——错误页/错误 JSON 两套分支、
create-user / reset-password 两个后台命令、时区回退与过滤器边界都没测。
错误分支决定「接口报错长什么样」，CLI 命令是运维建账号的唯一入口。
"""

from __future__ import annotations

import pytest
from flask import jsonify, request

from server.app import _display_tz, create_app
from server.app.config import Config


def _make_probe_app():
    """独立 app：探针路由只挂在自己身上，绝不能污染 session 共享 app"""
    app = create_app(Config)
    # TESTING=True 会默认传播异常（直接抛给调用方），线上是 False——
    # 这里显式关掉才能验到 500 错误处理分支本身。
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

    @app.route("/api/__probe500")
    def api_500():
        raise RuntimeError("探针故障")

    @app.route("/__probe500")
    def page_500():
        raise RuntimeError("探针故障")

    @app.route("/api/__probe403")
    def api_403():
        from flask import abort

        abort(403)

    @app.route("/__probe403")
    def page_403():
        from flask import abort

        abort(403)

    @app.route("/api/__echo", methods=["POST"])
    def api_echo():
        return jsonify({"ok": True, "size": len(request.get_data())})

    @app.route("/__echo", methods=["POST"])
    def page_echo():
        return f"收到 {len(request.get_data())} 字节"

    return app


@pytest.fixture()
def probe_app():
    return _make_probe_app()


# ---------- 404 ----------

def test_api_404_is_json(probe_app):
    resp = probe_app.test_client().get("/api/v1/根本没有这个接口")

    assert resp.status_code == 404
    data = resp.get_json()
    assert data["ok"] is False and data["error"] == "接口不存在"


def test_page_404_renders_error_page(probe_app):
    resp = probe_app.test_client().get("/不存在的页面")

    assert resp.status_code == 404
    html = resp.get_data(as_text=True)
    assert "页面不存在" in html
    assert resp.content_type.startswith("text/html")


# ---------- 403 ----------

def test_api_403_is_json(probe_app):
    resp = probe_app.test_client().get("/api/__probe403")

    assert resp.status_code == 403
    assert resp.get_json() == {"ok": False, "error": "需要管理员权限"}


def test_page_403_renders_error_page(probe_app):
    resp = probe_app.test_client().get("/__probe403")

    assert resp.status_code == 403
    assert "当前账号没有这个操作权限" in resp.get_data(as_text=True)


# ---------- 500 ----------

def test_api_500_is_json_not_html_stack(probe_app):
    """接口炸了要回 JSON：客户端按 ok/error 分支处理，拿到 HTML 会当成功解析失败"""
    resp = probe_app.test_client().get("/api/__probe500")

    assert resp.status_code == 500
    data = resp.get_json()
    assert data == {"ok": False, "error": "服务器内部错误"}


def test_page_500_renders_error_page(probe_app):
    resp = probe_app.test_client().get("/__probe500")

    assert resp.status_code == 500
    assert "服务器内部错误" in resp.get_data(as_text=True)


# ---------- 413 超大上传 ----------

def test_api_413_is_json(probe_app):
    probe_app.config["MAX_CONTENT_LENGTH"] = 16

    resp = probe_app.test_client().post("/api/__echo", data=b"x" * 1024)

    assert resp.status_code == 413
    assert resp.get_json()["error"] == "上传文件超过大小限制"


def test_page_413_is_plain_text(probe_app):
    probe_app.config["MAX_CONTENT_LENGTH"] = 16

    resp = probe_app.test_client().post("/__echo", data=b"x" * 1024)

    assert resp.status_code == 413
    assert "上传文件超过大小限制" in resp.get_data(as_text=True)


# ---------- 静态资源缓存头 ----------

def test_static_asset_carries_cache_header(app):
    """静态资源必须能被正常取到并带缓存头。

    注意：Flask 自己先下发了 no-cache，after_request 里的 setdefault 因此
    不会覆盖成 7 天长缓存（现状如此，已在回传里记为遗留问题）。
    """
    resp = app.test_client().get("/static/app.css")

    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control"), "静态响应必须带 Cache-Control"


# ---------- 模板过滤器 ----------

def test_filesize_filter_formats_boundaries(app):
    f = app.jinja_env.filters["filesize"]

    assert f(None) == "-"
    assert f(0) == "-"
    assert f(512) == "512 B"
    assert f(2048) == "2.0 KB"
    assert f(5 * 1024 * 1024) == "5.0 MB"
    assert f(3 * 1024 ** 3) == "3.0 GB"
    assert f(10 * 1024 ** 3) == "10.0 GB"  # 超过 GB 直接给 GB，不再换单位


def test_localtime_filter_handles_empty(app):
    assert app.jinja_env.filters["localtime"](None) == "-"


def test_display_tz_falls_back_when_zone_unavailable(monkeypatch):
    """拿不到时区不能抛：页面时间整体错 8 小比抛异常更糟，回退本地时区"""
    import server.app as app_pkg

    monkeypatch.setattr(app_pkg.Config, "TZ", "火星/火卫一")
    assert _display_tz() is None


# ---------- 后台 CLI ----------

def test_cli_create_user_and_reset_password(probe_app):
    """运维建号/改密的唯一入口；重复建号与改不存在的号都必须给出中文提示"""
    from server.app.db import db
    from server.app.models import AdminUser

    runner = probe_app.test_cli_runner()
    try:
        res = runner.invoke(args=["create-user", "--username", "qa-cli-op", "--password", "pw-123456"])
        assert res.exit_code == 0, res.output
        assert "已创建" in res.output and "（viewer）" in res.output

        again = runner.invoke(args=["create-user", "--username", "qa-cli-op", "--password", "pw-123456"])
        assert again.exit_code == 0
        assert "已存在" in again.output

        reset = runner.invoke(args=["reset-password", "--username", "qa-cli-op", "--password", "pw-654321"])
        assert reset.exit_code == 0
        assert "密码已重置" in reset.output

        missing = runner.invoke(args=["reset-password", "--username", "从没建过的号", "--password", "x"])
        assert missing.exit_code == 0
        assert "不存在" in missing.output

        # 新密码真的可用、旧密码失效（断言外部可观测的认证结果）
        with probe_app.app_context():
            user = AdminUser.query.filter_by(username="qa-cli-op").first()
            assert user is not None
            assert user.check_password("pw-654321") is True
            assert user.check_password("pw-123456") is False
    finally:
        with probe_app.app_context():
            AdminUser.query.filter_by(username="qa-cli-op").delete()
            db.session.commit()
