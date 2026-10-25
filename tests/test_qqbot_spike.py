"""scripts/qqbot_spike.py 的测试。

探针脚本跑一次要人去 QQ 里点一遍平台，跑错了就白折腾一轮，所以把「不需要网络
也能验的」部分钉死：

  - WebSocket 帧编解码：客户端帧必须带掩码、服务端裸帧（16/64 位长度）要能解、
    数据不齐时返回 None 而不是抛异常（要靠它做缓冲重入）
  - 控制帧要吃掉：ping 回 pong，close 当错误报出来，不能混进业务消息里
  - 官方错误码要翻成「去哪个页面点什么」——尤其 40034102（主动消息无权限）
  - access_token 的 expires_in 文档里是字符串 "7200"，int() 不能炸
  - 接口域名必须是 api.bot.qq.com（旧资料里的 api.sgroup.qq.com 已换）
"""

from __future__ import annotations

import importlib.util
import json
import socket
import struct
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("qqbot_spike", REPO / "scripts" / "qqbot_spike.py")
assert SPEC and SPEC.loader
spike = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spike)


def server_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """组一个**服务端**帧：不带掩码，长度按 7/16/64 位三档"""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        return head + bytes([n]) + payload
    if n < 65536:
        return head + bytes([126]) + struct.pack(">H", n) + payload
    return head + bytes([127]) + struct.pack(">Q", n) + payload


class FakeSock:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _n):
        if not self.chunks:
            raise socket.timeout()
        return self.chunks.pop(0)

    def settimeout(self, _t):
        pass

    def close(self):
        self.closed = True


def decoded_sent(ws) -> list[tuple[int, bytes]]:
    out = []
    for raw in ws.sock.sent:  # type: ignore[union-attr]
        frame = spike.decode_frame(raw)
        assert frame is not None, "发出去的帧自己都解析不了"
        out.append((frame[0], frame[1]))
    return out


# ==================== 帧编解码 ====================


def test_客户端帧必须带掩码():
    frame = spike.encode_frame(b'{"op":1}', 0x1)

    assert frame[1] & 0x80, "客户端帧不带掩码，服务端会直接断连"
    opcode, payload, rest = spike.decode_frame(frame)
    assert (opcode, payload, rest) == (0x1, b'{"op":1}', b"")


def test_能解_16_位与_64_位长度的服务端帧():
    medium = b"x" * 300
    large = b"y" * 70000

    for payload in (b"small", medium, large):
        opcode, got, rest = spike.decode_frame(server_frame(payload))
        assert opcode == 0x1
        assert got == payload, f"{len(payload)} 字节的帧解出来对不上"
        assert rest == b""


def test_数据不齐返回_none_而不是抛异常():
    frame = server_frame(b"hello")

    assert spike.decode_frame(b"") is None
    assert spike.decode_frame(frame[:1]) is None
    assert spike.decode_frame(frame[:-2]) is None
    # 补齐之后能解出来——缓冲重入靠的就是这个语义
    assert spike.decode_frame(frame)[1] == b"hello"


def test_一帧后面还有半帧时余下的字节要留着():
    both = server_frame(b"first") + server_frame(b"second")

    opcode, payload, rest = spike.decode_frame(both)
    assert payload == b"first"
    assert rest == server_frame(b"second"), "多读一个字节就会把下一帧吃坏"


# ==================== 控制帧 ====================


def test_ping_要回_pong_不能混进业务消息():
    ws = spike.MiniWs("wss://api.bot.qq.com/websocket/")
    ready = json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"user": {"username": "探针"}}})
    ws.sock = FakeSock([server_frame(b"", 0x9), server_frame(ready.encode())])

    assert ws.recv_text(0.2) == ready
    assert decoded_sent(ws) == [(0xA, b"")], "没回 pong，有的网关会因此断开"


def test_close_帧当错误抛出():
    ws = spike.MiniWs("wss://api.bot.qq.com/websocket/")
    ws.sock = FakeSock([server_frame(b"", 0x8)])

    with pytest.raises(spike.WsClosed):
        ws.recv_text(0.2)


def test_只接受_wss():
    with pytest.raises(ValueError):
        spike.MiniWs("ws://api.bot.qq.com/websocket/")


# ==================== 错误码翻译 ====================


def test_主动消息无权限要说清去点哪个开关():
    detail = spike.explain(400, {"code": 40034102, "message": "主动消息失败, 无权限"})

    assert "群设置" in detail
    assert "机器人主动在群聊内发言" in detail


@pytest.mark.parametrize(
    "status, body, needle",
    [
        (401, {"code": 11244}, "access_token"),
        (404, {"code": 404}, "group_openid"),
        (403, {"code": 403}, "权限配置"),
        (429, {}, "60 条/分"),
    ],
)
def test_其它错误码也要给人话(status, body, needle):
    assert needle in spike.explain(status, body)


def test_成功路径返回消息_id(monkeypatch):
    class Ok:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "MSGID123"}

    monkeypatch.setattr(spike.requests, "post", lambda *a, **k: Ok())
    ok, detail = spike.send_group_message("tok", "OPENID", "hi")

    assert ok and "MSGID123" in detail


# ==================== 凭证与入口 ====================


def test_接口域名锁定在_api_bot_qq_com():
    """旧资料写 api.sgroup.qq.com，官方 v2 文档已换成 api.bot.qq.com"""
    assert spike.DEFAULT_API_BASE == "https://api.bot.qq.com"


def test_有效期文档里是字符串也能解析(monkeypatch):
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "T", "expires_in": "7200"}

    monkeypatch.setattr(spike.requests, "post", lambda *a, **k: Resp())
    token, expires = spike.get_token("appid", "secret")

    assert token == "T"
    assert expires == 7200


def test_鉴权头是官方要求的_QQBot_前缀(monkeypatch):
    assert spike.api_headers("T")["Authorization"] == "QQBot T"


def test_缺凭证直接返回_2(monkeypatch):
    monkeypatch.delenv("QQ_BOT_APPID", raising=False)
    monkeypatch.delenv("QQ_BOT_SECRET", raising=False)

    assert spike.main(["--dry-run"]) == 2


def test_dry_run_验完凭证就停(monkeypatch):
    """dry-run 绝不能发消息：跑探针时群里的同事不该看到任何东西"""
    monkeypatch.setenv("QQ_BOT_APPID", "appid")
    monkeypatch.setenv("QQ_BOT_SECRET", "secret")
    monkeypatch.setattr(spike, "get_token", lambda *a, **k: ("T", 7200))
    monkeypatch.setattr(spike, "who_am_i", lambda *a, **k: {"id": "1", "username": "探针"})
    monkeypatch.setattr(spike, "send_group_message", lambda *a, **k: pytest.fail("dry-run 不该发消息"))

    assert spike.main(["--dry-run"]) == 0
