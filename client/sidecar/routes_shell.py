"""打开目录 / 日志 / 离线队列路由（拆自 server.py）：/open-folder /logs/open /queue*。

打开目录类接口路径只出本机固定目录或员工自己传的生成结果路径；
离线队列是记录上报的兜底（网络抖动/服务器 5xx 时暂存），
_client 由 create_sidecar 晚绑定传入（monkeypatch 兼容见 server.py）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request

from . import queue
from .config import load_config


def register_shell_routes(app: Flask, _client) -> None:
    # ========== 打开目录 / 日志 ==========
    @app.post("/open-folder")
    def open_folder():
        """生成后"打开文件夹"按钮"""
        cfg = load_config()
        raw = request.args.get("path") or cfg.output_dir
        target = Path(raw)
        if target.is_file():
            target = target.parent
        try:
            if sys.platform == "win32":
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                return jsonify({"ok": False, "error": "仅 Windows 支持"}), 400
            return jsonify({"ok": True})
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/logs/open")
    def open_log_dir():
        """打开日志目录：路径由服务端固定，不接受参数（免得变成任意目录打开器）"""
        from .config import app_data_dir

        log_dir = app_data_dir() / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return jsonify({"ok": False, "error": f"无法创建日志目录：{e}"}), 200
        if sys.platform != "win32":
            return jsonify({"ok": False, "error": "仅 Windows 支持"}), 200
        try:
            os.startfile(log_dir)  # type: ignore[attr-defined]
        except OSError as e:
            return jsonify({"ok": False, "error": f"打开失败：{e}"}), 200
        return jsonify({"ok": True, "path": str(log_dir)})

    # ========== 离线队列 ==========
    @app.get("/queue")
    def queue_status():
        return jsonify(
            {
                "ok": True,
                "pending": len(queue.pending()),
                "dead": len(queue.dead_letters()),
            }
        )

    @app.post("/queue/flush")
    def queue_flush():
        cfg = load_config()
        return jsonify({"ok": True, **queue.flush(_client(cfg))})

    @app.post("/queue/retry-dead")
    def queue_retry_dead():
        """把死信搬回队列并立刻试着补传一次。"""
        moved = queue.retry_dead()
        cfg = load_config()
        flushed = queue.flush(_client(cfg)) if moved["restored"] else {
            "sent": 0, "skipped": 0, "remaining": len(queue.pending()), "dead": moved["dead"],
        }
        return jsonify({"ok": True, "restored": moved["restored"], "failed": moved["failed"], **flushed})
