"""QQ 机器人全链路端到端验证（本地，不碰 NAS / 不连真 NapCat）

链路：模拟 NapCat 上报 → POST /api/v1/bot/onebot → handler → mock NapCat HTTP
（本脚本起的 39999）→ 断言收到的 send_private_msg / send_group_msg 内容。

同时覆盖：失败记录提交 → 即时告警推群；区间日报；帮助指令；自回声；
secret 鉴权；非指令静默。

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
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# ---- 隔离环境（必须在 import server.* 之前）----
_TMP = Path(tempfile.mkdtemp(prefix="psluyong-bot-e2e-"))
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["SECRET_KEY"] = "e2e-secret"
os.environ["API_KEY"] = "e2e-api-key-1234567890"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "e2e-admin-pass"
os.environ["ENABLE_SCHEDULER"] = "0"
os.environ["NAPCAT_WEBHOOK"] = "http://127.0.0.1:39999"
os.environ["NAPCAT_TOKEN"] = "e2e-token"
os.environ["REPORT_GROUP_ID"] = "134132323"
os.environ["BOT_ADMIN_QQ_IDS"] = ""  # 不限
os.environ["BOT_CALLBACK_SECRET"] = "e2e-cb-secret"
os.environ["ALERT_ON_FAILURE"] = "1"
os.environ["ALERT_MIN_INTERVAL_S"] = "0"  # 验证时不做间隔抑制

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("werkzeug").setLevel(logging.ERROR)

MOCK_PORT = 39999
SERVICE_PORT = 39998

# ---- mock NapCat：记录所有 OneBot 动作调用 ----
received: list[dict] = []


class _NapcatMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        received.append({"action": self.path, "payload": body,
                         "auth": self.headers.get("Authorization")})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","retcode":0}')

    def log_message(self, *args):  # 静音
        pass


def _seed_records(app):
    """造 3 天数据：昨天 3 条（1 失败）、今天 4 条"""
    from server.app.db import db
    from server.app.models import Record

    today = date.today()
    plan = [
        (today - timedelta(days=1), "农技推广中心", "农业加急", "failed", "PS 未响应"),
        (today - timedelta(days=1), "农技推广中心", "今日农业", "success", None),
        (today - timedelta(days=1), "农业大学", "中国收藏", "success", None),
        (today, "农技推广中心", "农业加急", "success", None),
        (today, "农业大学", "中国收藏", "success", None),
        (today, "农业科学院", "科学导报", "success", None),
        (today, "园艺研究所", "维普体育时空", "success", None),
    ]
    with app.app_context():
        for i, (d, partner, template, status, err) in enumerate(plan):
            db.session.add(Record(
                work_date=d, employee_name="测试员", template_name=template,
                partner_name=partner, author=f"作者{i}", title=f"标题{i}",
                status=status, error_message=err, processing_ms=1200,
            ))
        db.session.commit()
    return len(plan)


def _post_event(port: int, payload: dict, secret: str | None = "e2e-cb-secret") -> tuple[int, dict]:
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/bot/onebot",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"X-Bot-Secret": secret} if secret else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _event(text: str, user_id: int = 10001) -> dict:
    return {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "message_id": int(time.time()), "group_id": 888888, "user_id": user_id,
        "self_id": 20001, "time": int(time.time()),
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


def _wait(predicate, timeout_s=8.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


PASS, FAIL = "✅", "❌"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"{PASS if ok else FAIL} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


def main():
    mock = HTTPServer(("127.0.0.1", MOCK_PORT), _NapcatMock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    from waitress import serve
    from server.app import create_app
    from server.app.config import Config

    app = create_app(Config)
    app.config.update(TESTING=True)

    from server.app.db import db
    with app.app_context():
        db.create_all()
        n = _seed_records(app)
    print(f"已造测试数据 {n} 条（昨天 3 + 今天 4）")

    threading.Thread(target=lambda: serve(app, host="127.0.0.1", port=SERVICE_PORT, threads=4),
                     daemon=True).start()
    time.sleep(1.5)

    # ---------- 1. 鉴权 ----------
    code, _ = _post_event(SERVICE_PORT, _event("汇总"), secret=None)
    check("无密钥回调被拒（403）", code == 403, f"got {code}")
    code, body = _post_event(SERVICE_PORT, _event("汇总"), secret="wrong")
    check("错误密钥被拒（403）", code == 403, f"got {code}")
    code, _ = _post_event(SERVICE_PORT, _event("帮助", user_id=2001), secret="e2e-cb-secret")
    check("正确密钥放行（200）", code == 200, f"got {code}")

    # ---------- 2. 帮助指令 ----------
    received.clear()
    code, _ = _post_event(SERVICE_PORT, _event("帮助", user_id=2002))
    ok = _wait(lambda: any(r["action"] == "/send_private_msg" for r in received))
    check("帮助指令有私聊回复", ok)
    if ok:
        msg = received[-1]["payload"]["message"][0]["data"]["text"]
        check("帮助内容含指令表", "汇总 本周" in msg and "帮助" in msg)
        check("私聊目标正确", received[-1]["payload"]["user_id"] == 2002)

    # ---------- 3. 汇总（今天）----------
    received.clear()
    code, _ = _post_event(SERVICE_PORT, _event("汇总", user_id=2003))
    ok = _wait(lambda: any(r["action"] == "/send_private_msg" for r in received))
    check("「汇总」有私聊回复", ok)
    if ok:
        text = received[-1]["payload"]["message"][0]["data"]["text"]
        check("日报含今日标题", "期刊录用日报" in text)
        check("日报含总录用 4 条", "总录用：4 条" in text)
        check("日报含伙伴分布", "· 农技推广中心：1 条" in text)
        check("日报带群内触发标记", "群内触发" in text)

    # ---------- 4. 查询语义：不写归档 ----------
    reports_dir = _TMP / "data" / "reports"
    archived = list(reports_dir.glob("*.md")) if reports_dir.exists() else []
    check("群内触发不写归档文件", archived == [], f"found {archived}")

    # ---------- 5. 汇总昨天 ----------
    received.clear()
    _post_event(SERVICE_PORT, _event("汇总 昨天", user_id=2004))
    ok = _wait(lambda: any(r["action"] == "/send_private_msg" for r in received))
    check("「汇总 昨天」有回复", ok)
    if ok:
        text = received[-1]["payload"]["message"][0]["data"]["text"]
        check("昨天日报标题正确", str(date.today() - timedelta(days=1)) in text)
        check("昨天总录用 3 条", "总录用：3 条" in text)

    # ---------- 6. 汇总本周（区间 + 按日段）----------
    received.clear()
    _post_event(SERVICE_PORT, _event("汇总 本周", user_id=2005))
    ok = _wait(lambda: any(r["action"] == "/send_private_msg" for r in received))
    check("「汇总 本周」有回复", ok)
    if ok:
        text = received[-1]["payload"]["message"][0]["data"]["text"]
        check("区间标题正确", "期刊录用汇总" in text)
        check("区间总录用 7 条", "总录用：7 条" in text)
        check("含按日拆分段", "按日：" in text)

    # ---------- 7. 冷却 ----------
    received.clear()
    _post_event(SERVICE_PORT, _event("汇总"))
    first = _wait(lambda: any(r["action"] == "/send_private_msg" for r in received))
    received.clear()
    _post_event(SERVICE_PORT, _event("汇总"))
    time.sleep(1.0)
    check("冷却期内第二次触发无回复", first and not any(
        r["action"] == "/send_private_msg" for r in received))

    # ---------- 8. 自回声 ----------
    received.clear()
    payload = _event("汇总")
    payload["self_id"] = payload["user_id"]
    _post_event(SERVICE_PORT, payload)
    time.sleep(1.0)
    check("自回声被忽略", not any(r["action"] == "/send_private_msg" for r in received))

    # ---------- 9. 非指令静默 ----------
    received.clear()
    _post_event(SERVICE_PORT, _event("大家辛苦啦"))
    time.sleep(1.0)
    check("非指令消息静默", received == [])

    # ---------- 10. 失败即时告警（走真实记录提交接口）----------
    received.clear()
    import urllib.request

    boundary = "----e2e"
    parts = []
    for k, v in {"employee_name": "李四", "template_name": "农业加急",
                 "partner_name": "农技推广中心", "author": "王五",
                 "title": "端到端失败告警验证", "status": "failed",
                 "error_message": "Photoshop 未响应", "processing_ms": "900"}.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{SERVICE_PORT}/api/v1/records",
        data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "X-API-Key": os.environ["API_KEY"]},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        created = json.loads(resp.read())["ok"]
    check("失败记录提交成功（201）", created)
    ok = _wait(lambda: any(r["action"] == "/send_group_msg" for r in received))
    check("失败即时告警推群", ok)
    if ok:
        text = received[-1]["payload"]["message"][0]["data"]["text"]
        check("告警含失败原因", "Photoshop 未响应" in text)
        check("告警含员工名", "李四" in text)
        check("告警推到正确的群", received[-1]["payload"]["group_id"] == 134132323)

    # ---------- 11. 定时任务链路不受群内触发污染 ----------
    from bot.report import run_daily
    with app.app_context():
        result = run_daily(app, push=True)
    check("定时 run_daily 正常生成", result["pushed"] is True)
    status_file = _TMP / "data" / "reports" / f"{date.today().isoformat()}.status.json"
    check("定时推送状态落盘", status_file.exists())
    check("归档文件存在", (_TMP / "data" / "reports" / f"{date.today().isoformat()}.md").exists())

    # ---------- 12. 审计落库 ----------
    with app.app_context():
        from server.app.models import AuditLog
        entries = AuditLog.query.filter_by(action="bot_command").all()
    check("群内指令审计已写入", len(entries) >= 4, f"got {len(entries)}")
    actors = {e.actor for e in entries}
    check("审计 actor 是 qq: 号", "qq:10001" in actors)

    mock.shutdown()
    print()
    if _failures:
        print(f"❌ {len(_failures)} 项失败: {_failures}")
        sys.exit(1)
    print("🎉 全链路端到端验证全部通过")


if __name__ == "__main__":
    main()
