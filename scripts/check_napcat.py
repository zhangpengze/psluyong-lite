"""NapCat 连通性自检：读 .env 里的配置，真的调一次 get_login_info。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\check_napcat.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import urllib.error
import urllib.request

from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

webhook = os.getenv("NAPCAT_WEBHOOK", "").strip().rstrip("/")
token = os.getenv("NAPCAT_TOKEN", "").strip()
group = os.getenv("REPORT_GROUP_ID", "").strip()

print("=" * 56)
print(f"NAPCAT_WEBHOOK : {webhook or '(空)'}")
print(f"NAPCAT_TOKEN   : {(token[:4] + '***' + token[-4:]) if len(token) > 8 else (token or '(空)')}")
print(f"REPORT_GROUP_ID: {group or '(空)'}")
print("=" * 56)

if not webhook:
    print("[跳过] NAPCAT_WEBHOOK 为空，日报只会落盘不推送")
    sys.exit(1)

url = f"{webhook}/get_login_info"
req = urllib.request.Request(url)
if token:
    req.add_header("Authorization", f"Bearer {token}")

print(f"请求 {url} ...")
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", "replace")
    print(f"HTTP {resp.status}")
    print(body[:500])
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print("[警告] 返回不是 JSON，可能连到了别的服务（端口被占用？）")
        sys.exit(1)
    if data.get("status") == "ok" and data.get("data"):
        d = data["data"]
        print(f"\n[成功] 已登录 QQ: {d.get('nickname')} ({d.get('user_id')})")
        sys.exit(0)
    print(f"\n[失败] NapCat 返回: {data}")
    sys.exit(1)
except urllib.error.HTTPError as e:
    detail = e.read().decode("utf-8", "replace")[:300]
    print(f"HTTP {e.code}  {detail}")
    if e.code in (401, 403):
        print("\n[失败] 鉴权不通过：NAPCAT_TOKEN 与 NapCat「网络配置 → HTTP 服务端」里的 Token 不一致")
    sys.exit(1)
except urllib.error.URLError as e:
    print(f"[失败] 连不上：{e.reason}")
    print("排查：① Host 是否改成 0.0.0.0 且已启用 ② 端口映射 13000:3000 是否在 ③ 网络是否互通")
    sys.exit(1)
