"""测试夹具：所有用例跑在临时目录，不碰真实数据"""

from __future__ import annotations

import json
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
# 默认不接机器人：谁本机 .env 里配了真机器人，也不该让测试往群里发消息
for _key in ("QQ_BOT_APPID", "QQ_BOT_SECRET", "QQ_BOT_GROUP_OPENID", "QQ_BOT_API_BASE"):
    os.environ.pop(_key, None)

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


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """每条用例从空计数开始。

    限流是**跨请求**的状态：整个测试会话共用一个 app（同一来源 127.0.0.1），
    跑完几十条"提交记录"的用例之后，后面全部变成 429——失败信息还完全指错地方
    （看着像业务接口坏了）。生产里这个窗口只有 60 秒，测试里得手动清。
    """
    from server.app import ratelimit

    ratelimit._HITS.clear()
    yield


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


# ---------- 官方 QQ 机器人假接口 ----------
# 四个测试文件都要"配好机器人 + 不真发请求"，假响应放这里统一维护：
# 各写一份的结果是——某份只回 {"status":"ok"}，于是新加的鉴权分支永远测不到，
# 而用例照样全绿。


class Resp:
    """假响应。body=None 时 .json() 抛异常，用来模拟"不是 JSON"。"""

    def __init__(self, status_code: int = 200, body=None, text: str | None = None):
        self.status_code = status_code
        self.body = body
        self.text = text if text is not None else (
            json.dumps(body, ensure_ascii=False) if body is not None else ""
        )

    def json(self):
        if self.body is None:
            raise ValueError("Response is not JSON")
        return self.body


class FakeQqBot:
    """按 URL 分流"取 access_token"和"发群消息"，并把发出去的东西原样记下来。

    两个队列让用例能精确控制"第几次返回什么"：队列空了就一律成功。
    """

    def __init__(self):
        self.token_calls = 0
        self.urls: list[str] = []
        self.sent: list[dict] = []
        self.headers: list[dict] = []
        self.token_queue: list[Resp] = []
        self.send_queue: list[Resp] = []
        self.token_raise: Exception | None = None
        self.send_raise: Exception | None = None

    def post(self, url, json=None, headers=None, timeout=None):
        self.urls.append(url)
        if url.endswith("/app/getAppAccessToken"):
            self.token_calls += 1
            if self.token_raise is not None:
                raise self.token_raise
            if self.token_queue:
                return self.token_queue.pop(0)
            return Resp(200, {"access_token": "test-token", "expires_in": "7200"})
        if self.send_raise is not None:
            raise self.send_raise
        self.sent.append(json or {})
        self.headers.append(headers or {})
        if self.send_queue:
            return self.send_queue.pop(0)
        return Resp(200, {"id": "MSG-1", "timestamp": "2026-10-24T18:00:00+08:00"})

    # —— 造"下一次被拒"的便捷方法：不暴露 Resp，用例就不用 import 本模块 ——
    def refuse(self, err_code: int, message: str = "", status: int = 200) -> None:
        """下一次群发送被平台拒绝。默认 HTTP 200 + err_code：最容易漏判的形态。"""
        self.send_queue.append(Resp(status, {"err_code": err_code, "message": message}))

    def refuse_http(self, status: int, text: str = "") -> None:
        """下一次群发送是 HTTP 层失败（4xx/5xx），body 是纯文本"""
        self.send_queue.append(Resp(status, text=text or f"http {status}"))

    def refuse_legacy(self, code: int, message: str = "") -> None:
        """老版应答把失败码放在 code 里（不是 err_code），漏认就会假成功"""
        self.send_queue.append(Resp(200, {"code": code, "message": message}))

    def reply_html(self, status: int = 200, text: str = "<html>gateway</html>") -> None:
        """回一个不是 JSON 的 body（反代错误页），.json() 会抛异常"""
        self.send_queue.append(Resp(status, text=text))

    def refuse_token(self, status: int = 401, err_code: int = 40013, message: str = "invalid appid") -> None:
        self.token_queue.append(Resp(status, {"err_code": err_code, "message": message}))

    @property
    def texts(self) -> list[str]:
        """每次群发送带过去的 content"""
        return [m.get("content", "") for m in self.sent]

    @property
    def send_attempts(self) -> int:
        """真正打到"发消息"接口的次数（不含取 token、不含被异常吞掉的）"""
        return len([u for u in self.urls if u.endswith("/messages")])


@pytest.fixture()
def qqbot(monkeypatch):
    """配好一套假官方机器人，并把 requests.post 换成假接口。

    顺带把重试退避压成 0——不压的话每条"连不上"的用例要真睡 5+15 秒。
    """
    import requests as _requests

    from bot import qqbot_sender
    from server.app.config import Config

    fake = FakeQqBot()
    monkeypatch.setattr(_requests, "post", fake.post)
    monkeypatch.setattr(Config, "QQ_BOT_APPID", "test-appid")
    monkeypatch.setattr(Config, "QQ_BOT_SECRET", "test-secret")
    monkeypatch.setattr(Config, "QQ_BOT_GROUP_OPENID", "GROUP_OPENID_TEST")
    monkeypatch.setattr(Config, "QQ_BOT_API_BASE", "https://api.bot.qq.test")
    monkeypatch.setattr(qqbot_sender, "PUSH_BACKOFF_S", (0, 0))
    qqbot_sender.reset_token_cache()
    yield fake
    qqbot_sender.reset_token_cache()
