"""QQ 官方机器人可行性探针（spike）

回答一个问题：**把日报从 NapCat（登录真实 QQ 号）换成 QQ 官方机器人，行得通吗？**

不做改造，只做验证。四步各自给结论，任一步失败就停，并告诉你该去哪个
页面点什么：

  1. 换 access_token（AppID + AppSecret → access_token）
  2. 验凭证（GET /users/@me 能拿到机器人自己的名字）
  3. 学 group_openid（官方发群消息要的是 openid，**不是群号**，只能从事件里拿）
  4. 真发一条群消息（这一步才验得出「群设置里那个主动发言开关」有没有开）

用法：

    # 先在 .env 里填 QQ_BOT_APPID / QQ_BOT_SECRET
    .venv/Scripts/python.exe scripts/qqbot_spike.py

    # 已经有 group_openid（第二次以后），跳过第 3 步
    .venv/Scripts/python.exe scripts/qqbot_spike.py --group-openid XXXX

    # 只验凭证，不发消息
    .venv/Scripts/python.exe scripts/qqbot_spike.py --dry-run

第 3 步需要你在手机 QQ 里**在目标群里 @ 一下机器人**（或把机器人拉进群），
脚本挂着监听，收到事件就把 group_openid 打出来。

为什么自带一个 WebSocket 实现而不是装库：`requirements.txt` 的版本是锁死的
（那边注释写了原因，NAS 每次重建都重装依赖），为了一个探针脚本加一个依赖不划算。
这里只需要「连上、收几条文本帧、回心跳」这一小块，130 行够了。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

try:  # .env 里读 AppID/AppSecret，和项目其它脚本一致
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:  # noqa: BLE001 - 没有 .env 就靠环境变量，不该因此崩掉
    pass

# 官方文档：接口统一地址 https://api.bot.qq.com（旧文档里的 api.sgroup.qq.com 已换）
DEFAULT_API_BASE = "https://api.bot.qq.com"
# GROUP_AND_C2C_EVENT = 1 << 25，群 @ 消息、机器人被拉进群都在这一类里
INTENTS_GROUP = 1 << 25
TIMEOUT_S = 15


# ==================== 最小 WebSocket 客户端 ====================


class WsClosed(Exception):
    """连接被对端关闭"""


def mask_bytes(data: bytes, key: bytes) -> bytes:
    """RFC6455 掩码：客户端发出去的帧必须带掩码"""
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """组一个客户端帧（FIN=1、必须带掩码）"""
    head = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        head.append(0x80 | length)
    elif length < 65536:
        head.append(0x80 | 126)
        head += struct.pack(">H", length)
    else:
        head.append(0x80 | 127)
        head += struct.pack(">Q", length)
    key = os.urandom(4)
    return bytes(head) + key + mask_bytes(payload, key)


def decode_frame(buf: bytes) -> tuple[int, bytes, bytes] | None:
    """从缓冲区拆一个帧。数据不够返回 None。

    返回 (opcode, payload, 剩余字节)。服务端来的帧不带掩码，但按规范也支持带掩码的。
    """
    if len(buf) < 2:
        return None
    opcode = buf[0] & 0x0F
    masked = bool(buf[1] & 0x80)
    length = buf[1] & 0x7F
    offset = 2
    if length == 126:
        if len(buf) < offset + 2:
            return None
        length = struct.unpack(">H", buf[offset : offset + 2])[0]
        offset += 2
    elif length == 127:
        if len(buf) < offset + 8:
            return None
        length = struct.unpack(">Q", buf[offset : offset + 8])[0]
        offset += 8
    if masked:
        if len(buf) < offset + 4:
            return None
        key = buf[offset : offset + 4]
        offset += 4
    if len(buf) < offset + length:
        return None
    payload = buf[offset : offset + length]
    if masked:
        payload = mask_bytes(payload, key)
    return opcode, payload, buf[offset + length :]


class MiniWs:
    """只够用的 WebSocket：握手、发文本帧、收帧、回 pong"""

    def __init__(self, url: str, timeout: float = TIMEOUT_S):
        parsed = urlparse(url)
        if parsed.scheme != "wss":
            raise ValueError(f"只支持 wss://（拿到的是 {url}）")
        self.host = parsed.hostname or ""
        self.port = parsed.port or 443
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self.sock: ssl.SSLSocket | None = None
        self.buf = b""

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsClosed("握手阶段连接被关闭")
            head += chunk
        status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status_line:
            raise WsClosed(f"WebSocket 握手失败：{status_line}")
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def send_text(self, text: str) -> None:
        self._send(encode_frame(text.encode("utf-8"), 0x1))

    def _send(self, frame: bytes) -> None:
        if self.sock is None:
            raise WsClosed("还没连上")
        self.sock.sendall(frame)

    def recv_text(self, timeout_s: float) -> str | None:
        """收一条文本消息；超时返回 None。控制帧在这里消化掉。"""
        deadline = time.monotonic() + timeout_s
        while True:
            frame = decode_frame(self.buf)
            while frame is not None:
                opcode, payload, self.buf = frame
                if opcode == 0x1:
                    return payload.decode("utf-8")
                if opcode == 0x8:
                    raise WsClosed("对端发了 close 帧（通常是 intents 没权限）")
                if opcode == 0x9:  # ping -> pong
                    self._send(encode_frame(payload, 0xA))
                frame = decode_frame(self.buf)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self.sock is None:
                raise WsClosed("连接已关闭")
            self.sock.settimeout(min(remaining, 1.0))
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            except ssl.SSLError as exc:
                raise WsClosed(f"TLS 读失败：{exc}") from exc
            if not chunk:
                raise WsClosed("连接被对端断开")
            self.buf += chunk

    def close(self) -> None:
        if self.sock is not None:
            try:
                self._send(encode_frame(b"", 0x8))
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# ==================== 业务步骤 ====================


def api_headers(token: str) -> dict:
    # 官方格式：Authorization: QQBot {ACCESS_TOKEN}
    return {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}


def get_token(appid: str, secret: str, base: str = DEFAULT_API_BASE) -> tuple[str, int]:
    """AppID + AppSecret → access_token（有效期约 7200 秒）"""
    resp = requests.post(
        f"{base}/app/getAppAccessToken",
        json={"appId": appid, "clientSecret": secret},
        timeout=TIMEOUT_S,
    )
    body = safe_json(resp)
    token = (body or {}).get("access_token") or ""
    if not token:
        raise RuntimeError(f"换取 access_token 失败（HTTP {resp.status_code}）：{body}")
    # 文档说 expires_in 是 "7200" 这样的字符串，int() 兜住两种写法
    expires = int(str(body.get("expires_in") or 7200))
    return token, expires


def who_am_i(token: str, base: str = DEFAULT_API_BASE) -> dict:
    resp = requests.get(f"{base}/users/@me", headers=api_headers(token), timeout=TIMEOUT_S)
    if resp.status_code != 200:
        raise RuntimeError(f"GET /users/@me 失败（HTTP {resp.status_code}）：{safe_json(resp)}")
    return safe_json(resp) or {}


def gateway_url(token: str, base: str = DEFAULT_API_BASE) -> str:
    resp = requests.get(f"{base}/gateway", headers=api_headers(token), timeout=TIMEOUT_S)
    if resp.status_code != 200:
        raise RuntimeError(f"GET /gateway 失败（HTTP {resp.status_code}）：{safe_json(resp)}")
    url = (safe_json(resp) or {}).get("url") or ""
    if not url:
        raise RuntimeError(f"/gateway 没有返回 url：{safe_json(resp)}")
    return url


GROUP_EVENTS = {
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_ADD_ROBOT",
    "GROUP_MSG_RECEIVE",
}


def learn_group_openid(token: str, wss: str, wait_s: float, log=print) -> str | None:
    """连上 WebSocket，等一条群事件，从中抠出 group_openid

    官方发群消息要 group_openid（不是群号），而它只能从事件里学到——
    纯主动推送的系统第一次手上没有这个值，这就是第一步必须人配合的原因。
    """
    ws = MiniWs(wss)
    ws.connect()
    log(f"   已连上网关，等 Hello…")
    hello = ws.recv_text(10)
    if hello is None:
        raise RuntimeError("10 秒内没收到 Hello（op=10）")
    interval = 45.0
    try:
        interval = (json.loads(hello).get("d") or {}).get("heartbeat_interval", 45000) / 1000.0
    except (ValueError, AttributeError):
        pass

    ws.send_text(
        json.dumps(
            {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": INTENTS_GROUP,
                    "shard": [0, 1],
                    "properties": {},
                },
            }
        )
    )
    log(f"   已发送 Identify（intents={INTENTS_GROUP}），心跳间隔 {interval:.0f}s")
    log("   >>> 现在请在手机 QQ 的目标群里 @ 一下这个机器人 <<<")

    seq: int | None = None
    next_beat = time.monotonic() + interval
    deadline = time.monotonic() + wait_s
    ready = False
    while time.monotonic() < deadline:
        if time.monotonic() >= next_beat:
            ws.send_text(json.dumps({"op": 1, "d": seq}))
            next_beat = time.monotonic() + interval
        raw = ws.recv_text(min(1.0, max(0.1, deadline - time.monotonic())))
        if raw is None:
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if payload.get("s") is not None:
            seq = payload["s"]
        op = payload.get("op")
        if op == 9:
            raise RuntimeError(
                "服务端回了 Invalid Session（op=9）：intents 没权限。"
                "去开放平台「开发设置 → 权限配置」开通群聊消息相关权限"
            )
        if op == 0:
            event = payload.get("t")
            data = payload.get("d") or {}
            if event == "READY":
                ready = True
                log(f"   鉴权通过，session 已建立（{data.get('user', {}).get('username', '?')}）")
                continue
            if event in GROUP_EVENTS:
                openid = data.get("group_openid") or ""
                if openid:
                    log(f"   收到事件 {event}，group_openid = {openid}")
                    ws.close()
                    return openid
    ws.close()
    if not ready:
        log("   ⚠ 连上了但没鉴权成功，可能是 intents 或凭证问题")
    return None


def send_group_message(
    token: str, group_openid: str, content: str, seq: int = 1, base: str = DEFAULT_API_BASE
) -> tuple[bool, str]:
    """主动发一条群消息（不带 msg_id 就是主动消息，不是回复）"""
    resp = requests.post(
        f"{base}/v2/groups/{group_openid}/messages",
        headers=api_headers(token),
        json={"content": content, "msg_type": 0, "msg_seq": seq},
        timeout=TIMEOUT_S,
    )
    body = safe_json(resp)
    if resp.status_code == 200 and (body or {}).get("id"):
        return True, f"已发出，消息 id = {body['id']}"
    return False, explain(resp.status_code, body)


def safe_json(resp) -> dict | None:
    try:
        data = resp.json()
    except ValueError:
        return {"raw": resp.text[:300]}
    return data if isinstance(data, dict) else {"raw": data}


def explain(status: int, body) -> str:
    """把官方错误码翻译成「去哪个页面点什么」"""
    code = ""
    message = ""
    if isinstance(body, dict):
        code = str(body.get("code") or body.get("err_code") or "")
        message = str(body.get("message") or body.get("msg") or body.get("raw") or "")
    if code == "40034102" or "无权限" in message and "主动" in message:
        return (
            "主动消息无权限（40034102）。去手机 QQ：群设置 → 机器人 → 打开"
            "「机器人主动在群聊内发言」。这一步不开，日报就永远推不出去。"
        )
    if status == 401 or code in {"11244", "11245"}:
        return f"access_token 无效或已过期（HTTP {status} / {code}）。检查 AppID 与 AppSecret 是不是同一对。"
    if status == 404:
        return f"群不存在或机器人不在这个群里（HTTP 404 / {code}）。group_openid 对不上，重新跑第 3 步学一次。"
    if status == 403:
        return f"没有权限（HTTP 403 / {code}）。检查开放平台「开发设置 → 权限配置」是否开通群聊消息。"
    if status == 429:
        return f"触发频率限制（HTTP 429）。官方是 60 条/分·机器人，日报量级不该撞上，先重试一次。"
    return f"发送失败（HTTP {status} / code={code}）：{message[:300]}"


# ==================== 主流程 ====================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QQ 官方机器人可行性探针")
    parser.add_argument("--group-openid", default=os.getenv("QQ_BOT_GROUP_OPENID", ""))
    parser.add_argument("--wait", type=float, default=120.0, help="等群事件的秒数")
    parser.add_argument("--dry-run", action="store_true", help="只验凭证，不发消息")
    parser.add_argument("--text", default="", help="要发送的文本（默认一段探针文案）")
    parser.add_argument("--api-base", default=os.getenv("QQ_BOT_API_BASE", DEFAULT_API_BASE))
    args = parser.parse_args(argv)

    appid = os.getenv("QQ_BOT_APPID", "").strip()
    secret = os.getenv("QQ_BOT_SECRET", "").strip()
    if not appid or not secret:
        print("缺少 QQ_BOT_APPID / QQ_BOT_SECRET。先填进 .env：")
        print("  开放平台 → 你的机器人 → 开发设置 → 开发配置，拿 AppID 和 AppSecret")
        return 2

    print("[1/4] 换取 access_token")
    try:
        token, expires = get_token(appid, secret, args.api_base)
    except Exception as exc:  # noqa: BLE001 - 探针要的是人话，不是堆栈
        print(f"  失败：{exc}")
        return 1
    print(f"  成功，有效期约 {expires} 秒（脚本里没做缓存，正式接入要提前 60 秒刷新）")

    print("[2/4] 验凭证（GET /users/@me）")
    try:
        me = who_am_i(token, args.api_base)
    except Exception as exc:  # noqa: BLE001
        print(f"  失败：{exc}")
        return 1
    print(f"  成功，机器人名：{me.get('username') or me.get('nickname') or '?'}（id={me.get('id')}）")

    if args.dry_run:
        print("\n--dry-run：凭证没问题，后面的步骤跳过。")
        return 0

    group_openid = args.group_openid.strip()
    if not group_openid:
        print("[3/4] 学 group_openid（需要你在群里 @ 一下机器人）")
        try:
            wss = gateway_url(token, args.api_base)
            group_openid = learn_group_openid(token, wss, args.wait) or ""
        except Exception as exc:  # noqa: BLE001
            print(f"  失败：{exc}")
            return 1
        if not group_openid:
            print(f"  {args.wait:.0f} 秒内没收到群事件。检查：")
            print("   - 机器人真的在这个群里吗（是创建人当群主/管理员的群）")
            print("   - @ 的时候是不是真的 @ 到了（选的是机器人，不是同名成员）")
            print("   - 开放平台「开发设置 → 权限配置」开了群聊消息吗")
            return 1
        print(f"  学到 group_openid = {group_openid}")
        print(f"  建议写进 .env：QQ_BOT_GROUP_OPENID={group_openid}")
    else:
        print("[3/4] 跳过（已提供 group_openid）")

    text = args.text or (
        "【探针】这条消息由 psluyong-lite 的 qqbot 探针脚本发出。\n"
        "用途：验证官方机器人的「群主动消息」权限是否已开通。\n"
        "看到这条说明日报可以走官方机器人了；看不到说明群设置里的主动发言开关还没开。"
    )
    print("[4/4] 发一条群消息（主动消息）")
    ok, detail = send_group_message(token, group_openid, text, base=args.api_base)
    print(f"  {'成功' if ok else '失败'}：{detail}")
    if not ok:
        return 1
    print("\n结论：官方机器人这条路通。下一步可以先做双通道（NapCat 主、官方兜底），")
    print("再决定要不要把 NapCat 换掉——改造量见 docs/decisions/OPEN-DECISIONS.md。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
