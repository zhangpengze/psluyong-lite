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


def _diagnostics_dir() -> Path:
    """诊断包固定落在这里，方案一是"每次都在同一处"，员工好找、好重发。"""
    from .config import app_data_dir

    path = app_data_dir() / "diagnostics"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reveal(path: Path) -> None:
    """尽量把目录打开；失败不影响"诊断包已经生成"这个事实。"""
    if sys.platform != "win32":
        return
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except OSError:
        pass


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

    # ========== 诊断包 ==========
    def _build_diagnostics():
        from . import diagnostics

        try:
            client = _client(load_config())
        except Exception:  # noqa: BLE001 - 配置坏了也要能出包，这正是最需要它的时候
            client = None
        return diagnostics.build_bundle(client)

    @app.get("/diagnostics")
    def download_diagnostics():
        """诊断包的下载形态：给 curl / 远程排障用（界面走 /diagnostics/save）。"""
        from flask import send_file

        try:
            bundle = _build_diagnostics()
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"生成诊断包失败：{e}"}), 200
        return send_file(bundle, as_attachment=True, download_name=bundle.name)

    @app.post("/diagnostics/save")
    def save_diagnostics():
        """存到自己电脑的固定目录并打开，不依赖 webview 的下载能力。

        界面按钮走这条：Tauri 窗口里的 <a download> 在部分 Windows 版本上
        没反应，员工点了以为好了、其实什么也没拿到——比不给按钮更糟。
        """
        from . import diagnostics

        try:
            save_dir = _diagnostics_dir()
            bundle = _build_diagnostics()
            target = save_dir / bundle.name
            os.replace(bundle, target)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"生成诊断包失败：{e}"}), 200
        _reveal(target.parent)
        return jsonify({"ok": True, "path": str(target), "dir": str(target.parent)})

    # ========== 离线队列 ==========
    @app.get("/queue")
    def queue_status():
        blocked = queue.blocked_items()
        return jsonify(
            {
                "ok": True,
                "pending": len(queue.pending()),
                "dead": len(queue.dead_letters()),
                "pending_blocked": len(blocked),
                "blocked_reason": blocked[0]["reason"] if blocked else "",
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
            "blocked": len(queue.blocked_items()), "block_reason": "",
        }
        return jsonify({"ok": True, "restored": moved["restored"], "failed": moved["failed"], **flushed})
