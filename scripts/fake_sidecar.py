"""开发用假 sidecar：给界面截图/核对布局喂固定数据，不读也不写真实配置。

    .venv/Scripts/python.exe scripts/fake_sidecar.py         # 监听 127.0.0.1:8765

只实现前端我会用到的那些路由，返回内容和真 sidecar 同构。
好处：截图时数据可控（模板几个、记录几条、在线还是离线），
而且完全不碰 %APPDATA% 里的真实 config.json，截图脚本可以随便跑。

时间口径照员工机器来（+08:00）：`work_date` 是本地日期、`created_at` 是 UTC，
和真服务端一致。**不用进程所在的时区**——后台启动时常量 TZ 会被环境重置，
那样截出来的「今天」会比界面差一天（踩过：工作日期 10-23、界面显示 10-24，
分组标题变成「昨天」，看着像代码 bug 其实是假数据的锅）。
要换时区就设 FAKE_TZ_OFFSET（小时，可含小数）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from flask import Flask, Response, jsonify, request
from PIL import Image, ImageDraw

app = Flask(__name__)

# 假数据的世界时区：默认 +08:00。放在这里而不是靠运行环境，见文件头说明。
LOCAL_TZ = timezone(timedelta(hours=float(os.environ.get("FAKE_TZ_OFFSET", "8"))))


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _today() -> date:
    return _now().date()

# 与 client/sidecar/server.py 的 ALLOWED_ORIGINS 保持一致，
# 否则 vite dev（5173）里前端所有请求都会被浏览器拦掉，截出来只有错误卡片
ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
}


@app.after_request
def _cors(resp):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


CONFIG = {
    "server_url": "http://192.168.1.10:8080",
    "api_key": "demo-key-0000",
    "employee_name": "张三",
    "ps_path": "C:/Program Files/Adobe/Adobe Photoshop 2023/Photoshop.exe",
    "output_dir": "D:/期刊录用",
    "jpg_quality": 10,
    "auto_clear": True,
    "last_template_id": "t1",
    "last_partner": "华东出版社",
    "last_author": "陈明",
    "partners": ["华东出版社", "南方文化", "示例合作伙伴"],
    "template_dir": "D:/期刊录用/模板",
    "author_placeholder": "{{作者}}",
    "title_placeholder": "{{标题}}",
    "filename_rule": "author_title",
    "always_on_top": False,
    "dock_side": "",
    "auto_dock": True,
    "close_to_tray": True,
    "win_x": -1,
    "win_y": -1,
    "history_win_x": -1,
    "history_win_y": -1,
    "settings_win_x": -1,
    "settings_win_y": -1,
    "theme": "dark",
}

TEMPLATES = [
    {"id": "t1", "name": "录用通知模板", "filename": "录用通知模板.psd", "size": 24 * 1024 * 1024},
    {"id": "t2", "name": "封面模板", "filename": "封面模板.psd", "size": 18 * 1024 * 1024},
    {"id": "t3", "name": "证书横版", "filename": "证书横版.psd", "size": 9 * 1024 * 1024},
]

_TITLES = [
    "基于深度学习的图像分割方法研究",
    "面向边缘计算的任务调度优化",
    "多模态大模型的知识蒸馏",
    "城市轨道交通客流预测",
    "碳交易市场价格波动分析",
]
_AUTHORS = ["陈明", "刘洋", "赵敏", "周涛", "孙悦"]


# (几天前, 几点)：故意跨月跨年地铺开，才能验证「今天 / 近 7 天 / 近 30 天 /
# 全部 / 自选日期」这几档的差别，也能看出时间列到底有没有年、有没有被切掉
_WHEN = [
    (0, 9),
    (0, 7),
    (1, 15),
    (3, 11),
    (6, 16),
    (12, 10),
    (20, 14),
    (40, 9),
    (75, 11),
    (150, 15),
    (300, 10),
    (400, 12),
    (460, 9),
]


def _history() -> list[dict]:
    now = _now()
    items = []
    rid = 100
    for i, (days_ago, hour) in enumerate(_WHEN):
        rid -= 1
        created = (now - timedelta(days=days_ago)).replace(hour=hour, minute=(i * 7) % 60, second=0)
        if days_ago == 0:
            # 今天的两条按"几十分钟前"生成：固定小时数在凌晨跑会变成未来时间
            created = now - timedelta(minutes=20 + 40 * i)
        # work_date 取本地日期，created_at 发 UTC（前端 parseTime 会补 Z 再转本地）
        created_utc = created.astimezone(timezone.utc).replace(tzinfo=None)
        bad = i == 7
        items.append(
            {
                "id": rid,
                "work_date": created.strftime("%Y-%m-%d"),
                "template_name": TEMPLATES[i % 3]["name"],
                "partner_name": CONFIG["partners"][i % 3] if i % 4 else "",
                "author": _AUTHORS[i % 5],
                "title": _TITLES[i % 5],
                "status": "failed" if bad else "success",
                "has_image": not bad,
                "created_at": created_utc.isoformat(),
                "error_message": "PS 未响应，已超时" if bad else None,
                "processing_ms": 0 if bad else 12000 + i * 1300,
                "client_machine": "DESKTOP-DEMO",
                "ps_version": "25.0",
                "image_filename": "" if bad else f"{_AUTHORS[i % 5]}_{_TITLES[i % 5]}.jpg",
            }
        )
    return items


def _png(seed: int, w: int = 360, h: int = 240) -> bytes:
    """画一张像录用通知的图，便于判断缩略图是否正常"""
    img = Image.new("RGB", (w, h), (250, 249, 245))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 34], fill=(37, 99, 235))
    d.text((12, 11), "JOURNAL ACCEPTANCE", fill=(255, 255, 255))
    d.text((24, 70), "录用通知", fill=(30, 30, 30))
    d.text((24, 100), f"No.{seed:04d}  基于深度学习的图像分割方法研究", fill=(60, 60, 60))
    d.rectangle([24, 170, w - 24, 172], fill=(210, 210, 210))
    d.text((24, 182), "示例出版社", fill=(90, 90, 90))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def _ps_info() -> dict:
    """Photoshop 识别结果。

    直接用真机的识别逻辑（而不是写死一个假路径）：这块代码的效果本来就
    "取决于这台机器装了什么"，喂个假路径只会让截图好看、上线才发现问题。
    真机没装时给一个写死的样例，保证界面上有东西可看。
    """
    try:
        import sys
        from pathlib import Path

        root = str(Path(__file__).resolve().parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from client.sidecar.ps_detect import detect_photoshop

        info = detect_photoshop(CONFIG.get("ps_path", ""))
        if info.found:
            return info.to_dict()
    except Exception:  # noqa: BLE001 - 假 sidecar 不该因为探测失败而挂
        pass
    return {
        "path": r"C:\Program Files\Adobe\Adobe Photoshop 2023\Photoshop.exe",
        "version": "24.7.0",
        "source": "scan",
        "source_label": "安装目录扫描",
        "found": True,
        "override_missing": False,
    }


@app.get("/status")
def status():
    return jsonify(
        {
            "ok": True,
            "server_reachable": True,
            "com_available": True,
            "platform": "Windows-10",
            "ps": _ps_info(),
            "config": CONFIG,
            "pending": 0,
            "dead": 0,
            "busy": False,
            "machine": "DESKTOP-DEMO",
            "version": "1.0.0",
            "log_dir": "C:/Users/Administrator/AppData/Roaming/psluyong-lite/logs",
        }
    )


@app.post("/ps/detect")
def detect_ps():
    return jsonify({"ok": True, "ps": _ps_info()})


@app.get("/config")
def get_config():
    return jsonify({"ok": True, "config": CONFIG})


@app.post("/config")
def save_config():
    CONFIG.update(request.get_json(force=True, silent=True) or {})
    return jsonify({"ok": True, "config": CONFIG})


@app.get("/templates")
def templates():
    return jsonify({"ok": True, "items": TEMPLATES, "template_dir": CONFIG["template_dir"]})


@app.post("/templates/upload")
def upload():
    return jsonify({"ok": True, "saved": [], "errors": [], "items": TEMPLATES})


@app.post("/templates/delete")
def delete_template():
    return jsonify({"ok": True, "items": TEMPLATES})


@app.post("/templates/open-dir")
def open_dir():
    return jsonify({"ok": True, "path": CONFIG["template_dir"]})


@app.get("/dicts")
def dicts():
    return jsonify({"ok": True, "grouped": {"partner": CONFIG["partners"]}})


@app.get("/history")
def history():
    """语义与 client/sidecar/server.py 的 /history 保持一致：
    today/7/30/all + custom，并且回显真正生效的 start/end。"""
    today = _today()
    rng = request.args.get("range", "7")
    start = end = None

    def _d(name):
        raw = (request.args.get(name) or "").strip()
        try:
            return date.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    if rng == "custom":
        start, end = _d("start"), _d("end")
        if start and not end:
            end = today
        elif end and not start:
            start = end
        if not start or not end:
            rng = "7"

    if rng != "custom":
        if rng == "today":
            start = end = today
        elif rng == "all":
            start, end = date(2000, 1, 1), today
        else:
            days = int(rng) if rng in ("7", "30") else 7
            rng = str(days)
            start, end = today - timedelta(days=days - 1), today

    if start > end:
        start, end = end, start
    size = 200 if (end - start).days >= 30 else 60

    items = [
        it for it in _history() if start.isoformat() <= it["work_date"] <= end.isoformat()
    ]
    return jsonify(
        {
            "ok": True,
            "items": items,
            "total": len(items),
            "range": rng,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "size": size,
        }
    )


@app.get("/history/<int:rid>/image")
def image(rid: int):
    full = request.args.get("full") == "1"
    data = _png(rid, 900, 600) if full else _png(rid)
    return Response(data, mimetype="image/jpeg")


@app.post("/queue/flush")
def flush():
    return jsonify({"ok": True, "sent": 0, "skipped": 0, "remaining": 0, "dead": 0})


@app.post("/queue/retry-dead")
def retry_dead():
    return jsonify({"ok": True, "restored": 0, "failed": 0, "sent": 0, "skipped": 0, "remaining": 0, "dead": 0})


@app.post("/open-folder")
def open_folder():
    return jsonify({"ok": True})


@app.post("/logs/open")
def open_logs():
    return jsonify({"ok": True, "path": ""})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(json.dumps({"fake_sidecar": f"http://127.0.0.1:{port}"}), flush=True)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
