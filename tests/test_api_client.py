"""ApiClient 传输层测试（离线打桩，不发任何真实请求）

审计缺口：api_client.py 只有 30%——全部走 happy path，离线/超时/非 JSON/
4xx/取图失败这些「员工真的会遇到」的分支一条都没测。断言的都是外部可观测
值：翻译后的中文报错、状态码、payload、超时时长、文件句柄是否关闭。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from client.sidecar import api_client as api_mod
from client.sidecar.api_client import DEFAULT_TIMEOUT, UPLOAD_TIMEOUT, ApiClient, ServerError


class _Resp:
    def __init__(self, status_code: int = 200, payload=None, raw_text: str | None = None,
                 content: bytes = b""):
        self.status_code = status_code
        self._payload = payload
        self._raw_text = raw_text
        self.content = content

    def json(self):
        if self._raw_text is not None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture()
def api() -> ApiClient:
    # 尾斜杠必须被吃掉，否则拼出 http://x//api/v1/... 反而 404
    return ApiClient("http://127.0.0.1:8080/", "test-key")


def _stub_request(monkeypatch, exc=None, resp: _Resp | None = None):
    """替换 requests.request：要么抛异常，要么返回替身；记录调用参数"""
    calls: list[dict] = []

    def fake(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        if exc is not None:
            raise exc
        return resp

    monkeypatch.setattr(api_mod.requests, "request", fake)
    return calls


# ---------- 连接层错误翻译 ----------

def test_connection_error_translates_to_chinese_message(api, monkeypatch):
    """断网/服务没起：员工看到的是「连不上服务器」，不是 Python 堆栈"""
    _stub_request(monkeypatch, exc=requests.ConnectionError("refused"))

    with pytest.raises(ServerError) as info:
        api._request("GET", "/api/v1/records")

    assert "连不上服务器" in info.value.message
    assert info.value.status == 0  # 没有 HTTP 响应，状态码必须是 0 而不是 200


def test_timeout_translates_to_retry_message(api, monkeypatch):
    _stub_request(monkeypatch, exc=requests.Timeout("read timed out"))

    with pytest.raises(ServerError) as info:
        api._request("GET", "/api/v1/records")

    assert "超时" in info.value.message


def test_default_timeout_is_passed_to_requests(api, monkeypatch):
    """默认 10s 超时必须真的传给 requests，否则离线时客户端会无限挂着"""
    calls = _stub_request(monkeypatch, resp=_Resp(payload={"ok": True}))

    api._request("GET", "/health")

    assert calls[0]["timeout"] == DEFAULT_TIMEOUT
    assert calls[0]["headers"] == {"X-API-Key": "test-key"}
    assert calls[0]["url"] == "http://127.0.0.1:8080/health"


# ---------- 响应层错误翻译 ----------

def test_non_json_body_becomes_empty_payload_error(api, monkeypatch):
    """HTML 错误页（网关 502 之类）不能让 json() 抛出去炸掉调用方"""
    _stub_request(monkeypatch, resp=_Resp(status_code=200, raw_text="<html>502</html>"))

    with pytest.raises(ServerError) as info:
        api._request("POST", "/api/v1/records")

    assert info.value.status == 200
    assert "服务器返回 200" in info.value.message  # ok 缺失 = 失败


def test_401_surfaces_server_error_text(api, monkeypatch):
    """API Key 被吊销：服务端给的 error 原文要能透出给排障的人"""
    _stub_request(
        monkeypatch,
        resp=_Resp(status_code=401, payload={"ok": False, "error": "API Key 无效"}),
    )

    with pytest.raises(ServerError) as info:
        api._request("GET", "/api/v1/records")

    assert info.value.status == 401
    assert info.value.message == "API Key 无效"
    assert info.value.payload["ok"] is False


def test_500_without_error_key_falls_back_to_status_text(api, monkeypatch):
    _stub_request(monkeypatch, resp=_Resp(status_code=500, payload={"ok": False}))

    with pytest.raises(ServerError) as info:
        api._request("GET", "/api/v1/records")

    assert info.value.message == "服务器返回 500"
    assert info.value.status == 500


def test_ok_false_with_200_is_still_an_error(api, monkeypatch):
    """业务失败常以 200 + ok=false 返回：状态码 200 不能被当成成功"""
    _stub_request(
        monkeypatch,
        resp=_Resp(status_code=200, payload={"ok": False, "error": "记录校验失败"}),
    )

    with pytest.raises(ServerError) as info:
        api._request("POST", "/api/v1/records")

    assert info.value.message == "记录校验失败"
    assert info.value.status == 200


def test_success_returns_full_payload(api, monkeypatch):
    _stub_request(monkeypatch, resp=_Resp(payload={"ok": True, "data": {"id": 7}}))

    assert api._request("GET", "/x") == {"ok": True, "data": {"id": 7}}


# ---------- 记录提交 ----------

def test_submit_record_sends_all_fields_as_strings(api, monkeypatch):
    """服务端只收字符串：None 必须变 ""，数字必须变文本，否则表单解析炸"""
    calls = _stub_request(monkeypatch, resp=_Resp(payload={"ok": True, "data": {"id": 11}}))

    data = api.submit_record({"author": "李四", "note": None, "qty": 3}, image_path=None)

    assert data == {"id": 11}
    sent = calls[0]["data"]
    assert sent == {"author": "李四", "note": "", "qty": "3"}
    assert calls[0]["files"] is None
    assert calls[0]["timeout"] == UPLOAD_TIMEOUT  # 大图要单独放宽超时


def test_submit_record_opens_and_closes_image_handle(api, monkeypatch, tmp_path):
    """图片句柄必须在请求后关闭——否则 Windows 上文件被占着删不掉"""
    img = tmp_path / "成品图.png"
    img.write_bytes(b"PNGDATA")

    captured: dict = {}

    def fake(method, url, **kwargs):
        captured["files"] = kwargs["files"]
        return _Resp(payload={"ok": True, "data": {"id": 1}})

    monkeypatch.setattr(api_mod.requests, "request", fake)

    api.submit_record({"author": "李四"}, image_path=img)

    name, handle, ctype = captured["files"]["image"]
    assert name == "成品图.png"
    assert ctype == "application/octet-stream"
    assert handle.closed, "请求结束后文件句柄必须关闭"


def test_submit_record_missing_image_sends_no_file(api, monkeypatch, tmp_path):
    """图还没落盘就提交：不带文件而不是带着一个打不开的路径"""
    calls = _stub_request(monkeypatch, resp=_Resp(payload={"ok": True, "data": {}}))

    result = api.submit_record({"author": "李四"}, image_path=tmp_path / "不存在.png")

    assert result == {}
    assert calls[0]["files"] is None


def test_submit_record_failure_still_closes_handle(api, monkeypatch, tmp_path):
    """提交失败（500）也不能漏句柄"""
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    captured: dict = {}

    def fake(method, url, **kwargs):
        captured["files"] = kwargs["files"]
        return _Resp(status_code=500, payload={"ok": False, "error": "服务器炸了"})

    monkeypatch.setattr(api_mod.requests, "request", fake)

    with pytest.raises(ServerError) as info:
        api.submit_record({"author": "李四"}, image_path=img)

    assert info.value.message == "服务器炸了"
    assert captured["files"]["image"][1].closed


# ---------- 健康检查 ----------

def test_health_true_when_server_answers(api, monkeypatch):
    monkeypatch.setattr(api_mod.requests, "get", lambda url, timeout, **kwargs: _Resp())
    assert api.health() is True


def test_health_false_on_any_request_exception(api, monkeypatch):
    def boom(url, timeout, **kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(api_mod.requests, "get", boom)
    assert api.health() is False


# ---------- 历史查询 ----------

def test_list_records_builds_full_date_window(api, monkeypatch):
    """不带 start/end 就只能看到服务端默认的 7 天——窗口必须显式传"""
    calls = _stub_request(
        monkeypatch,
        resp=_Resp(payload={"ok": True, "items": [{"id": 1}], "total": "42"}),
    )

    result = api.list_records("李四", size=30, start="2026-10-01", end="2026-10-24")

    params = calls[0]["params"]
    assert params == {"size": 30, "employee": "李四", "start": "2026-10-01", "end": "2026-10-24"}
    assert result == {"items": [{"id": 1}], "total": 42}  # total 是字符串也要转成 int


def test_list_records_omits_blank_filters(api, monkeypatch):
    """空员工/空日期不能变成 employee= 传过去，否则服务端过滤条件错"""
    calls = _stub_request(monkeypatch, resp=_Resp(payload={"ok": True}))

    result = api.list_records("", size=60)

    assert calls[0]["params"] == {"size": 60}
    assert result == {"items": [], "total": 0}  # 缺 items/total 时给安全默认值


# ---------- 取图 ----------

def test_fetch_image_sends_api_key_and_thumb_flag(api, monkeypatch):
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None, **kwargs):
        seen.update(url=url, headers=headers, params=params, timeout=timeout)
        return _Resp(status_code=200, content=b"JPEGDATA")

    monkeypatch.setattr(api_mod.requests, "get", fake_get)

    assert api.fetch_image(123, thumb=True) == b"JPEGDATA"
    assert seen["url"] == "http://127.0.0.1:8080/api/v1/records/123/image"
    assert seen["headers"] == {"X-API-Key": "test-key"}
    assert seen["params"] == {"thumb": "1"}
    assert seen["timeout"] == DEFAULT_TIMEOUT


def test_fetch_image_full_size_drops_thumb_param(api, monkeypatch):
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None, **kwargs):
        seen["params"] = params
        return _Resp(status_code=200, content=b"b")

    monkeypatch.setattr(api_mod.requests, "get", fake_get)

    api.fetch_image(5, thumb=False)

    assert seen["params"] == {}


def test_fetch_image_missing_record_raises_with_status(api, monkeypatch):
    monkeypatch.setattr(
        api_mod.requests,
        "get",
        lambda url, headers=None, params=None, timeout=None, **kwargs: _Resp(status_code=404),
    )

    with pytest.raises(ServerError) as info:
        api.fetch_image(999)

    assert info.value.message == "图片不存在"
    assert info.value.status == 404


def test_fetch_image_network_failure_is_translated(api, monkeypatch):
    def boom(url, headers=None, params=None, timeout=None, **kwargs):
        raise requests.ConnectionError("gone")

    monkeypatch.setattr(api_mod.requests, "get", boom)

    with pytest.raises(ServerError) as info:
        api.fetch_image(1)

    assert "取图失败" in info.value.message


# ---------- 系统代理 ----------

def test_record_upload_bypasses_system_proxy(api, monkeypatch, tmp_path):
    """上报记录这条最不能出事：公司电脑上配了 HTTP_PROXY 时，
    请求若被送去代理，代理回 502，这条成品就永远传不上去——
    而运维在自己机器上（没配代理）复现不出来。"""
    calls = _stub_request(monkeypatch, resp=_Resp(payload={"ok": True, "data": {}}))
    api.submit_record({"author": "李四"}, None)
    assert calls[0]["proxies"] == {"http": None, "https": None}


def test_fetch_image_bypasses_system_proxy(api, monkeypatch):
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None, **kwargs):
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(api_mod.requests, "get", fake_get)
    api.fetch_image(1)
    assert seen["proxies"] == {"http": None, "https": None}


def test_health_bypasses_system_proxy(api, monkeypatch):
    seen = {}

    def fake_get(url, timeout=None, **kwargs):
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(api_mod.requests, "get", fake_get)
    api.health()
    assert seen["proxies"] == {"http": None, "https": None}
