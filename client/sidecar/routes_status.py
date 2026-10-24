"""状态与配置路由（拆自 server.py）：/health /status /ps /ps/detect /config。

client_of 是 create_sidecar 传入的晚绑定 client 工厂——测试
monkeypatch.setattr(server, "_client", ...) 必须在请求时生效，
所以这里只通过参数拿到它，不在模块级 import 具体实现。
"""

from __future__ import annotations

import logging
import platform
import sys

from flask import Flask, jsonify, request

from core.ps_connector import COM_AVAILABLE  # noqa: E402

from . import __version__, queue
from .config import load_config, sanitize_config_patch, save_config
from .ps_info import _resolve_ps

logger = logging.getLogger(__name__)


def register_status_routes(app: Flask, executor, _client) -> None:
    # ========== 状态 ==========
    @app.get("/health")
    def health():
        return jsonify({"ok": True, "com_available": COM_AVAILABLE, "platform": sys.platform})

    @app.get("/status")
    def status():
        cfg = load_config()
        client = _client(cfg)
        from .config import app_data_dir

        return jsonify(
            {
                "ok": True,
                "server_reachable": client.health(),
                "com_available": COM_AVAILABLE,
                "platform": platform.platform(),
                # 本机 Photoshop 的自动识别结果：路径/版本/怎么找到的。
                # 员工不必再手填路径——装哪版、装在哪个盘都能认出来
                "ps": _resolve_ps(cfg).to_dict(),
                "config": cfg.to_dict(),
                "pending": len(queue.pending()),
                "dead": len(queue.dead_letters()),
                "machine": platform.node(),
                "busy": executor.running_task() is not None,
                # 版本与日志路径：员工报障时直接复制走，不用再问"你装的哪版"
                "version": __version__,
                "log_dir": str(app_data_dir() / "logs"),
            }
        )

    # ========== Photoshop 自动识别 ==========
    @app.get("/ps")
    def get_ps():
        """当前机器上的 Photoshop（手填优先，否则自动识别）。"""
        return jsonify({"ok": True, "ps": _resolve_ps(load_config()).to_dict()})

    @app.post("/ps/detect")
    def detect_ps():
        """重新识别：装完 PS / 换版本后点一下"重新识别"就走这里。

        故意不做成写配置：识别是运行时的事，把结果落到 ps_path 会让
        "自动识别"悄悄变成"写死路径"，员工下次换版本反而对不上。
        """
        return jsonify({"ok": True, "ps": _resolve_ps(load_config()).to_dict()})

    # ========== 配置 ==========
    @app.get("/config")
    def get_config():
        return jsonify({"ok": True, "config": load_config().to_dict()})

    @app.post("/config")
    def set_config():
        payload = request.get_json(silent=True) or {}
        cfg = load_config()
        clean, errors = sanitize_config_patch(payload)
        if errors:
            # 整包拒绝：只写一半会让配置文件处在"部分生效"的状态，
            # 员工看到的界面和实际行为对不上，比直接报错难查得多
            logger.warning("配置保存被拒绝: %s", "；".join(errors))
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "；".join(errors),
                        "config": cfg.to_dict(),
                    }
                ),
                400,
            )
        for key, value in clean.items():
            setattr(cfg, key, value)
        save_config(cfg)
        logger.info("配置已保存 employee=%s server=%s", cfg.employee_name, cfg.server_url)
        return jsonify({"ok": True, "config": cfg.to_dict()})
