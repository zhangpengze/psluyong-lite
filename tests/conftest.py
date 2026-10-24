"""测试夹具：所有用例跑在临时目录，不碰真实数据"""

from __future__ import annotations

import os
import shutil
import tempfile
from io import BytesIO
from pathlib import Path

# 必须在导入 server.app.config 之前设置——模块级常量在 import 时就固化了
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="psluyong-lite-test-"))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("API_KEY", "test-api-key-1234567890")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin-pass-123")
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["ENABLE_SCHEDULER"] = "0"
os.environ["PSLUYONG_DATA_DIR"] = str(_TEST_DATA_DIR / "client")
os.environ.pop("NAPCAT_WEBHOOK", None)

import pytest  # noqa: E402

from server.app import create_app  # noqa: E402
from server.app.config import Config  # noqa: E402
from server.app.db import db  # noqa: E402

API_KEY = os.environ["API_KEY"]


@pytest.fixture(scope="session")
def app():
    application = create_app(Config)
    application.config.update(TESTING=True)
    yield application
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture()
def client(app):
    return app.test_client()


def post_login(client, username="admin", password="admin-pass-123", environ_base=None):
    """带 CSRF 的 /login POST。

    登录表单挂了 CSRF 校验：先 GET /login 让会话拿到 token，
    再从会话里取出来放进 _csrf 字段提交——与浏览器真实登录路径一致。
    """
    kwargs = {"environ_base": environ_base} if environ_base else {}
    client.get("/login", **kwargs)
    with client.session_transaction() as sess:
        token = sess.get("csrf_token", "")
    data = {"username": username, "password": password, "_csrf": token}
    return client.post("/login", data=data, **kwargs)


@pytest.fixture()
def admin_client(app):
    """已登录的后台会话"""
    c = app.test_client()
    resp = post_login(c)
    assert resp.status_code == 302, "管理员登录失败"
    return c


@pytest.fixture()
def admin_csrf(admin_client):
    """已登录会话 + 从页面 meta 取到的 CSRF 请求头。

    模拟后台真实写操作：先渲染页面拿 token，再带 X-CSRF-Token 调写接口。
    """
    import re

    html = admin_client.get("/dashboard").get_data(as_text=True)
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert match, "后台页面缺少 CSRF meta 标签"
    return {"X-CSRF-Token": match.group(1)}


@pytest.fixture()
def clean_db(app):
    """每个用例前清空业务表，避免用例间互相污染"""
    with app.app_context():
        from server.app.models import Record

        Record.query.delete()
        db.session.commit()
    yield


@pytest.fixture()
def sample_image():
    """生成测试图的工厂。每次调用返回新的 BytesIO——复用同一个会在
    werkzeug 第一次读取后关闭，第二次提交直接 ValueError。"""
    from PIL import Image

    def make(width: int = 800, height: int = 500):
        buf = BytesIO()
        Image.new("RGB", (width, height), (37, 99, 235)).save(buf, "PNG")
        buf.seek(0)
        return buf

    return make
