"""打开目录 / 日志 / 离线队列路由（拆自 server.py）：/open-folder /logs/open /queue*。

打开目录类接口路径只出本机固定目录或员工自己传的生成结果路径；
离线队列是记录上报的兜底（网络抖动/服务器 5xx 时暂存），
_client 由 create_sidecar 晚绑定传入（monkeypatch 兼容见 server.py）。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request

from . import queue
from .config import load_config

logger = logging.getLogger(__name__)


def _resolve(path: Path) -> Path:
    """尽力规范化成绝对真实路径。盘符不存在 / 权限不足时 resolve() 会抛，
    退回 absolute()——比这里炸掉更合理，后面的白名单比较仍然有效。"""
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _allowed_open_roots(cfg) -> list[Path]:
    """「打开文件夹」允许落地的根目录：输出目录、模板目录、本机数据目录。

    只放这三个地方，是因为这个按钮的所有正当用途都在里面：生成完打开成品
    所在目录、记录页打开输出目录、诊断包/日志目录。多放一个根，就多一条
    "把本机任意目录（含系统目录）打开给调用方看"的路。
    """
    from .config import app_data_dir

    roots: list[Path] = []
    for raw in (cfg.output_dir, getattr(cfg, "template_dir", "")):
        text = (raw or "").strip()
        if text:
            roots.append(_resolve(Path(text)))
    roots.append(_resolve(app_data_dir()))
    return roots


def _is_inside(target: Path, roots: list[Path]) -> bool:
    """target 是否落在某个允许的根目录里（根本身也算）。

    比的是规范化后的绝对路径，所以 `..\\..\\Windows` 这类串没用；
    Windows 路径大小写不敏感，用 normcase 归一，否则 `D:\\期刊录用` 与
    `d:\\期刊录用` 会被判成两个地方。
    """
    real = os.path.normcase(str(_resolve(target)))
    for root in roots:
        base = os.path.normcase(str(root))
        if real == base or real.startswith(base + os.sep):
            return True
    return False


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
        """生成后"打开文件夹"按钮。

        路径来自前端（成品图所在目录），所以必须卡白名单：本机 sidecar 绑在
        127.0.0.1，任何能在这台机器上发 POST 的程序（哪怕只是网页里的一段
        fetch）都能借这个接口把任意目录打开在员工屏幕上。危害不算大，但一条
        白名单就堵住了，没有理由不做。
        """
        cfg = load_config()
        raw = (request.args.get("path") or cfg.output_dir or "").strip()
        if not raw:
            return jsonify({"ok": False, "error": "还没设置输出目录"}), 400
        target = Path(raw)
        if target.is_file():
            target = target.parent
        if not _is_inside(target, _allowed_open_roots(cfg)):
            logger.warning("拒绝打开白名单外的目录：%s", raw)
            return jsonify(
                {"ok": False, "error": "只能打开本机的输出目录、模板目录或日志目录"}
            ), 400
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
        flushed = queue.flush(_client(cfg)) if moved["restored"] else queue.empty_result()
        return jsonify({"ok": True, "restored": moved["restored"], "failed": moved["failed"], **flushed})
