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
from .config import (
    ConfigBusyError,
    last_recovery,
    load_config,
    sanitize_config_patch,
    update_config,
)
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

        blocked = queue.blocked_items()

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
                # 被环境/配置拦住的积压（401/403/404）：必须让员工看见原因，
                # 否则"待补传 N 条"永远不减少，而根因在设置里的 API Key 或服务器地址
                "pending_blocked": len(blocked),
                "blocked_reason": blocked[0]["reason"] if blocked else "",
                "machine": platform.node(),
                "busy": executor.running_task() is not None,
                # 版本与日志路径：员工报障时直接复制走，不用再问"你装的哪版"
                "version": __version__,
                "log_dir": str(app_data_dir() / "logs"),
                # 配置被自愈过（损坏→从备份恢复 / 重置为默认值）：
                # 空字符串表示没事。界面只在非空时提示一次，免得员工
                # 对着"服务器离线"瞎猜
                "config_recovered": last_recovery(),
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
                        "config": load_config().to_dict(),
                    }
                ),
                400,
            )
        # 读-改-写在同一把锁里：窗口位置记忆、主题切换、别的窗口保存
        # 可能正同时进来，分成 load/save 两步会互相覆盖
        try:
            cfg = update_config(clean)
        except ConfigBusyError as e:
            # 配置文件正被别的程序占着：宁可报"稍后重试"，也不能拿默认值
            # 回写把员工的服务器地址和 Key 冲掉
            logger.warning("配置保存失败：%s", e)
            return jsonify({"ok": False, "error": str(e)}), 503
        logger.info("配置已保存 employee=%s server=%s", cfg.employee_name, cfg.server_url)
        return jsonify({"ok": True, "config": cfg.to_dict()})
