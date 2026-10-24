"""登录 CSRF 与登出改 POST（P2 安全加固批次）

三条防线：
  - 无 CSRF token 的 /login POST 必须被拒（防跨站伪造登录/登录 CSRF）
  - /logout 只收 POST，GET 返回 405——跨站 <img src="/logout"> 不能再强制登出
  - POST /logout 同样挂 csrf_protect，隐藏字段 _csrf 是浏览器真实路径
"""

from __future__ import annotations

from tests.conftest import post_login


def test_login_form_includes_csrf_field(client):
    """登录页表单必须渲染 _csrf 隐藏字段，否则正常用户也会被 403 挡住"""
    html = client.get("/login").get_data(as_text=True)
    assert 'name="_csrf"' in html
    assert 'value="' in html


def test_login_post_without_csrf_rejected(client):
    """跨站表单能替受害者提交凭据，但拿不到 session 里的 token"""
    resp = client.post("/login", data={"username": "admin", "password": "admin-pass-123"})
    assert resp.status_code == 403
    # 会话没有被建立：访问后台仍被踢回登录页
    assert "/login" in client.get("/dashboard").headers.get("Location", "")


def test_login_post_with_wrong_csrf_rejected(client):
    client.get("/login")
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "admin-pass-123", "_csrf": "wrong-token"},
    )
    assert resp.status_code == 403
    assert "/login" in client.get("/dashboard").headers.get("Location", "")


def test_login_post_with_csrf_succeeds(client):
    """带 token 的登录照旧成功：加固不能把正常登录挡在门外"""
    assert post_login(client).status_code == 302
    assert client.get("/dashboard").status_code == 200


def test_logout_get_not_allowed(admin_client):
    """写操作不得走 GET：img/iframe 标签就能触发的 URL 不该存在"""
    resp = admin_client.get("/logout")
    assert resp.status_code == 405
    # 会话没被动过
    assert admin_client.get("/dashboard").status_code == 200


def test_logout_post_requires_csrf(admin_client):
    resp = admin_client.post("/logout")
    assert resp.status_code == 403
    # 未登出
    assert admin_client.get("/dashboard").status_code == 200


def test_logout_post_form_csrf(admin_client, admin_csrf):
    """真实浏览器路径：侧栏表单的 _csrf 隐藏字段"""
    resp = admin_client.post("/logout", data={"_csrf": admin_csrf["X-CSRF-Token"]})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert "/login" in admin_client.get("/dashboard").headers.get("Location", "")


def test_sidebar_logout_entry_is_post_form(admin_client):
    """侧栏登出入口必须是 POST 表单，全仓不残留 GET 登出链接"""
    html = admin_client.get("/dashboard").get_data(as_text=True)
    assert 'action="/logout"' in html
    assert 'name="_csrf"' in html
    assert 'href="/logout"' not in html
