"""官方机器人全链路端到端验证（本地，不碰 NAS、不连真平台）

链路：真 HTTP 提交一条失败记录 → 即时告警 → 官方接口（本脚本起的 39999 假平台）
→ 断言发出去的 content / 鉴权头 / 目标 openid；再跑 run_daily 验证
生成 → 存档 → 推送 → 状态落盘 → 幂等跳过。

为什么还要这层：pytest 里的假接口是 monkeypatch 掉 requests 的，测不到
"配置从 .env 到 api_base() 再到 URL 拼接"这一段；而线上最常配错的就是这段
（域名、group_openid、APPID/SECRET）。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\verify_bot_e2e.py

环境完全隔离：临时 DATA_DIR + 临时 sqlite，不读 .env、不碰 server/data。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# ---- 隔离环境（必须在 import server.* 之前）----
_TMP = Path(tempfile.mkdtemp(prefix="psluyong-bot-e2e-"))
MOCK_PORT = 39999
SERVICE_PORT = 39998
GROUP_OPENID = "GROUP_OPENID_E2E"

os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["SECRET_KEY"] = "e2e-secret"
os.environ["API_KEY"] = "e2e-api-key-1234567890"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "e2e-admin-pass"
os.environ["ENABLE_SCHEDULER"] = "0"
os.environ["QQ_BOT_APPID"] = "e2e-appid"
os.environ["QQ_BOT_SECRET"] = "e2e-secret"
os.environ["QQ_BOT_GROUP_OPENID"] = GROUP_OPENID
os.environ["QQ_BOT_API_BASE"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["ALERT_ON_FAILURE"] = "1"
os.environ["ALERT_MIN_INTERVAL_S"] = "0"  # 验证时不做间隔抑制

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ---- 假平台：记录每次调用 ----
received: list[dict] = []
# 设成非 0 时，下一次发消息返回 HTTP 200 + err_code（模拟"没有主动发言权限"）
reject_with: dict | None = None


class _PlatformMock(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        received.append(
            {"path": self.path, "body": body, "auth": self.headers.get("Authorization")}
        )
        if self.path == "/app/getAppAccessToken":
            reply = {"access_token": "e2e-access-token", "expires_in": "7200"}
        elif reject_with is not None:
            reply = reject_with
        else:
            reply = {"id": "MSG-E2E", "timestamp": "2026-10-24T18:00:00+08:00"}
        payload = json.dumps(reply, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静音
        pass


PASS, FAIL = "✅", "❌"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


def _sends() -> list[dict]:
    return [r for r in received if r["path"].endswith("/messages")]


def _wait(predicate, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _submit_failed_record() -> int:
    """走真实接口提交一条失败记录（触发即时告警）"""
    import urllib.request

    boundary = "----e2e"
    fields = {
        "employee_name": "李四",
        "template_name": "农业加急",
        "partner_name": "农技推广中心",
        "author": "王五",
        "title": "端到端失败告警验证",
        "status": "failed",
        "error_message": "Photoshop 未响应",
        "processing_ms": "900",
    }
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    ]
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{SERVICE_PORT}/api/v1/records",
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-API-Key": os.environ["API_KEY"],
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def main() -> None:
    global reject_with

    mock = HTTPServer(("127.0.0.1", MOCK_PORT), _PlatformMock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    from waitress import serve

    from server.app import create_app
    from server.app.config import Config

    app = create_app(Config)
    app.config.update(TESTING=True)

    from server.app.db import db

    with app.app_context():
        db.create_all()

    threading.Thread(
        target=lambda: serve(app, host="127.0.0.1", port=SERVICE_PORT, threads=4), daemon=True
    ).start()
    time.sleep(1.5)

    # ---------- 1. 失败记录 → 即时告警 ----------
    status = _submit_failed_record()
    check("失败记录提交成功（201）", status == 201, f"got {status}")
    check("告警已发到群里", _wait(lambda: len(_sends()) >= 1), f"sends={len(_sends())}")
    if _sends():
        first = _sends()[0]
        check("证据：鉴权头是 QQBot 前缀", first["auth"] == "QQBot e2e-access-token", first["auth"])
        check(
            "证据：目标是最新的 group_openid",
            first["path"] == f"/v2/groups/{GROUP_OPENID}/messages",
            first["path"],
        )
        check("证据：msg_type=0（纯文本）", first["body"].get("msg_type") == 0, str(first["body"]))
        text = first["body"].get("content", "")
        check("告警含失败原因", "Photoshop 未响应" in text)
        check("告警含员工名", "李四" in text)
        check("取 token 用的是 POST appId/clientSecret",
              any(r["path"] == "/app/getAppAccessToken"
                  and r["body"].get("appId") == "e2e-appid" for r in received))

    # ---------- 2. 定时日报：生成 → 存档 → 推送 → 状态落盘 ----------
    from bot.report import run_daily

    before = len(_sends())
    with app.app_context():
        result = run_daily(app, push=True)
    check("定时日报推送成功", result["pushed"] is True, result["message"])
    check("真的发出一条群消息", len(_sends()) == before + 1, f"{before} -> {len(_sends())}")

    reports_dir = _TMP / "data" / "reports"
    day = date.today().isoformat()
    check("日报已存档", (reports_dir / f"{day}.md").exists())
    status_file = reports_dir / f"{day}.status.json"
    check("推送状态已落盘", status_file.exists())
    if status_file.exists():
        saved = json.loads(status_file.read_text("utf-8"))
        check("状态里记的是已推送", saved.get("pushed") is True, str(saved))

    # ---------- 3. 幂等：同样的数据再跑一次不该重复推 ----------
    before = len(_sends())
    with app.app_context():
        again = run_daily(app, push=True)
    check("重复运行被幂等闸门挡下", again.get("skipped") is True, str(again.get("message")))
    check("没有多发一条", len(_sends()) == before)

    # ---------- 4. 平台拒发时必须是"失败"，不是"成功" ----------
    # 群没开「机器人主动在群聊内发言」时平台回 HTTP 200 + err_code 40034102。
    # 这一条是本系统的老伤：只看状态码 → 徽标绿 → 兜底告警永远不触发。
    reject_with = {"err_code": 40034102, "message": "主动消息失败, 无权限"}
    before = len(_sends())
    with app.app_context():
        bad = run_daily(app, push=True, force=True)
    reject_with = None
    check("平台拒发时判为未推送", bad["pushed"] is False, f"pushed={bad['pushed']}")
    check("拒发只尝试一次（不重试）", len(_sends()) == before + 1, f"{before} -> {len(_sends())}")
    saved = json.loads(status_file.read_text("utf-8"))
    check("状态落盘写的是失败", saved.get("pushed") is False, str(saved.get("message")))
    check("失败原因带错误码", "40034102" in str(saved.get("message")), str(saved.get("message")))
    check("失败原因翻成了能照着做的中文",
          "机器人主动在群聊内发言" in str(saved.get("message")), str(saved.get("message")))

    # ---------- 5. 超长日报：发的是截断版，存的是全文 ----------
    from bot import report as report_mod

    huge = "\n".join(f"  · 合作伙伴{i}：模板{i}：{i} 条" for i in range(300))
    original_render = report_mod.render
    report_mod.render = lambda *a, **k: huge
    try:
        before = len(_sends())
        with app.app_context():
            long_run = run_daily(app, push=True, force=True)
    finally:
        report_mod.render = original_render
    sent_text = _sends()[-1]["body"].get("content", "") if len(_sends()) > before else ""
    check("超长日报仍然推送成功", long_run["pushed"] is True, long_run["message"])
    check("发出去的是截断版", "已省略" in sent_text)
    check("存档是全文",
          Path(long_run["path"]).read_text("utf-8") == huge)

    # ---------- 6. 仪表盘：配了机器人就不该显示"未配置" ----------
    from server.app.status import system_status

    with app.app_context():
        report_row = system_status()["rows"]["report"]
    check("仪表盘日报行不是「未配置」", report_row["status"] != "off", str(report_row))
    check("仪表盘日报行说的是推送结果",
          "推送" in report_row["detail"], str(report_row))

    mock.shutdown()
    print()
    if _failures:
        print(f"{FAIL} {len(_failures)} 项失败: {_failures}")
        sys.exit(1)
    print("🎉 全链路端到端验证全部通过")


if __name__ == "__main__":
    main()
