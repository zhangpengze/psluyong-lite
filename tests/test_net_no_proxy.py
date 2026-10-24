"""client/sidecar/net.py：对内网的请求必须绕开系统代理。

这个模块存在的唯一理由是一个很难查的故障：员工机器上装了 VPN 客户端 / IT 推了
系统代理，于是 `HTTP_PROXY` 被设上，`requests` 把发给内网服务器的请求送去代理，
代理回 502。员工侧永远"连接失败"，运维侧永远正常。

所以这里钉死两件事：
  1. 环境变量里有代理，我们也照样直连
  2. 调用方显式要代理时，我们不抢它的设置
"""

from __future__ import annotations

import pytest
import requests

from client.sidecar import net

BYPASS = {"http": None, "https": None}


class _Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return "RESPONSE"


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(requests, "get", rec)
    monkeypatch.setattr(requests, "request", rec)
    return rec


@pytest.fixture()
def corporate_proxy(monkeypatch):
    """模拟公司电脑：三个代理变量全都设上"""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:54392")


def test_get_bypasses_env_proxy(recorder, corporate_proxy):
    net.get("http://192.168.1.10:8080/health", timeout=3)
    assert recorder.calls[0]["kwargs"]["proxies"] == BYPASS


def test_request_bypasses_env_proxy(recorder, corporate_proxy):
    net.request("POST", "http://192.168.1.10:8080/api/v1/records", json={"a": 1})
    assert recorder.calls[0]["kwargs"]["proxies"] == BYPASS


def test_caller_can_still_ask_for_a_proxy(recorder):
    """把"绕开代理"做成默认值，而不是硬规定：真有需要代理的调用方不该被拦"""
    net.get("http://example.com", proxies={"http": "http://proxy:8080"})
    assert recorder.calls[0]["kwargs"]["proxies"] == {"http": "http://proxy:8080"}


def test_each_call_gets_its_own_proxy_dict(recorder):
    """requests 会往传进去的 proxies 字典里 setdefault——
    共用一个字典迟早被上一次调用污染。"""
    net.get("http://a")
    net.get("http://b")
    first, second = recorder.calls[0]["kwargs"]["proxies"], recorder.calls[1]["kwargs"]["proxies"]
    assert first == second == BYPASS
    assert first is not second


def test_returns_what_requests_returns(recorder):
    assert net.get("http://a") == "RESPONSE"


# ---------- 真实的 requests 也要认这个姿态 ----------

def test_bypass_dict_actually_beats_env_for_requests(monkeypatch):
    """光断言"传了 proxies"不够：得确认 requests 真按这个值办事。

    直接问 requests 自己：给了 proxies={"http": None} 时，它算出来的代理应该是空的，
    而不是从环境变量补一个回来。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:54392")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:54392")
    session = requests.Session()
    settings = session.merge_environment_settings("http://192.168.1.10:8080", dict(BYPASS), None, None, None)
    assert settings["proxies"].get("http") is None

    env_only = session.merge_environment_settings("http://192.168.1.10:8080", {}, None, None, None)
    assert env_only["proxies"].get("http") == "http://127.0.0.1:54392", (
        "这条是反证：不传 proxies 时 requests 会把环境代理补上——正是要防的事"
    )
