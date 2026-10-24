"""测试连接：必须测「表单里此刻填的」地址和 Key，而不是磁盘上的旧配置。

这条 bug 的表现特别气人：改了服务器地址 → 点"测试连接" → 提示连接成功（还是
新地址）→ 保存 → 第一条记录 401。因为实现里打的是 `/status`，它读的是磁盘配置；
员工越测越自信。
"""

from __future__ import annotations

import requests

from client.sidecar import conn_test


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _Recorder:
    """记录 requests.get 的调用，并按 URL 决定返回什么"""

    def __init__(self, health_code=200, auth_code=200, health_exc=None, auth_exc=None):
        self.calls: list[tuple[str, dict]] = []
        self.proxies: list = []
        self.health_code = health_code
        self.auth_code = auth_code
        self.health_exc = health_exc
        self.auth_exc = auth_exc

    def __call__(self, url, headers=None, params=None, timeout=None, **kwargs):
        # **kwargs 不能省：conn_test 走的是 net.get，会多传一个 proxies
        # （绕开系统代理）。recorder 签名太窄的话，异常会被 probe 的
        # 兜底 except 吃掉，全都变成"测试连接出错"，看着像功能坏了。
        self.proxies.append(kwargs.get("proxies"))
        self.calls.append((url, headers or {}))
        if url.endswith("/health"):
            if self.health_exc:
                raise self.health_exc
            return _Resp(self.health_code)
        if self.auth_exc:
            raise self.auth_exc
        return _Resp(self.auth_code)


# ---------- 地址格式 ----------

def test_empty_url_is_invalid():
    r = conn_test.probe("", "k")
    assert r["level"] == "invalid" and "服务器地址" in r["message"]
    assert r["server_reachable"] is False


def test_url_without_scheme_is_invalid():
    r = conn_test.probe("192.168.1.10:8080", "k")
    assert r["level"] == "invalid" and "http://" in r["message"]


# ---------- 地址不通 ----------

def test_connection_error_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        requests, "get", _Recorder(health_exc=requests.ConnectionError("refused"))
    )
    r = conn_test.probe("http://10.0.0.9:8080", "k")
    assert r["level"] == "unreachable"
    assert "连不上" in r["message"] and "10.0.0.9" in r["message"], "要带上具体地址"
    assert r["key_ok"] is False


def test_ssl_error_is_reported_separately(monkeypatch):
    monkeypatch.setattr(
        requests, "get", _Recorder(health_exc=requests.exceptions.SSLError("bad cert"))
    )
    r = conn_test.probe("https://nas.local", "k")
    assert r["level"] == "unreachable" and "证书" in r["message"]


# ---------- Key 不对 ----------

def test_401_means_key_is_wrong_not_address(monkeypatch):
    """地址是通的（/health 200），只是 Key 无效——这两句话不能混。"""
    rec = _Recorder(auth_code=401)
    monkeypatch.setattr(requests, "get", rec)

    r = conn_test.probe("http://10.0.0.9:8080/", "old-key")

    assert r["level"] == "key_bad"
    assert r["server_reachable"] is True and r["key_ok"] is False
    assert "API Key" in r["message"]
    assert rec.calls[-1][1]["X-API-Key"] == "old-key", "必须用传进来的 Key，不是磁盘里的"
    assert rec.calls[-1][0].startswith("http://10.0.0.9:8080/api/v1/records"), "不许出现双斜杠"


def test_403_is_also_key_bad(monkeypatch):
    monkeypatch.setattr(requests, "get", _Recorder(auth_code=403))
    assert conn_test.probe("http://x", "k")["level"] == "key_bad"


def test_404_means_wrong_server_not_wrong_key(monkeypatch):
    """地址指向了别的系统 / 老版本没有这个接口——文案要说到这一层"""
    monkeypatch.setattr(requests, "get", _Recorder(auth_code=404))
    r = conn_test.probe("http://x", "k")
    assert r["level"] == "key_bad" and "接口" in r["message"]


def test_500_is_server_error_not_key(monkeypatch):
    monkeypatch.setattr(requests, "get", _Recorder(auth_code=500))
    r = conn_test.probe("http://x", "k")
    assert r["level"] == "server_error" and "500" in r["message"]


# ---------- 都通 ----------

def test_ok_round_trip(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(requests, "get", rec)

    r = conn_test.probe("http://127.0.0.1:8080/", "good-key")

    assert r["level"] == "ok" and r["key_ok"] is True and r["server_reachable"] is True
    assert r["message"].endswith("（http://127.0.0.1:8080）"), "结尾的斜杠要归一化"
    assert [c[0] for c in rec.calls] == [
        "http://127.0.0.1:8080/health",
        "http://127.0.0.1:8080/api/v1/records?size=1",
    ], "先测地址再测鉴权：顺序错了就分不清是哪一环不通"


def test_key_with_spaces_is_stripped(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(requests, "get", rec)
    conn_test.probe("http://x", "  k  ")
    assert rec.calls[-1][1]["X-API-Key"] == "k"


def test_probe_never_raises_even_on_unexpected_error(monkeypatch):
    """这是给按钮用的：任何异常都要变成一句人话，界面不能卡在「测试中…」"""

    def boom(*_a, **_kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(requests, "get", boom)
    r = conn_test.probe("http://x", "k")
    assert r["level"] == "unreachable"
    assert "RuntimeError" in r["message"], "意外异常也要说清类型，便于员工截图报障"


# ---------- 系统代理 ----------

def test_probe_bypasses_system_proxy(monkeypatch):
    """公司电脑上配了 HTTP_PROXY 时，"测试连接"不能被送去代理。

    代理不认识内网地址，会回一个 502 英文错误页；员工看到的是"服务器返回 502"，
    于是去找运维，运维在自己机器上（没配代理）怎么试都正常。2026-10-24 真踩过。
    """
    rec = _Recorder()
    monkeypatch.setattr(requests, "get", rec)
    conn_test.probe("http://10.0.0.9:8080", "k")
    assert len(rec.proxies) == 2, "两个探针（/health 与鉴权）都要发出去"
    for sent in rec.proxies:
        assert sent == {"http": None, "https": None}, f"没有绕开代理：{sent}"
