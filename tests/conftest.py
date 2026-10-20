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


@pytest.fixture()
def admin_client(app):
    """已登录的后台会话"""
    c = app.test_client()
    resp = c.post("/login", data={"username": "admin", "password": "admin-pass-123"})
    assert resp.status_code == 302, "管理员登录失败"
    return c


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
