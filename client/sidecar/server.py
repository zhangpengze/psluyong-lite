"""
员工客户端 sidecar

Tauri 前端通过 HTTP 调它（只绑 127.0.0.1）。为什么用 HTTP 而不是 stdio JSON-RPC：
Python 侧只需要一个 Flask，前端用 fetch 就能调，省掉 Rust 侧的协议桥接代码；
风险面靠"只绑回环 + CORS 白名单"收敛。

本文件只做装配：安全钩子在 security.py，业务路由按资源拆在 routes_*.py，
文件系统判断在 file_ops.py，Photoshop 识别在 ps_info.py，任务结算在 settler.py
（create_sidecar 保持无副作用，后台结算线程只在 main 里启动，测试才好控）。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# 允许从 client/sidecar 直接运行：把仓库根加进 sys.path 才能 import core
# （必须先于任何 import core 的子模块执行——routes_* 在下方 import）
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from flask import Flask  # noqa: E402

from . import __version__  # noqa: E402,F401  重新导出：测试与外部读 sidecar_server.__version__
from . import settler  # noqa: E402
from .api_client import ApiClient  # noqa: E402
from .config import ClientConfig, ensure_dirs  # noqa: E402
from .executor import Executor  # noqa: E402
from .file_ops import MAX_TEMPLATE_BYTES  # noqa: E402
from .routes_export import register_export_routes  # noqa: E402
from .routes_history import register_history_routes  # noqa: E402
from .routes_shell import register_shell_routes  # noqa: E402
from .routes_status import register_status_routes  # noqa: E402
from .routes_templates import register_template_routes  # noqa: E402
from .security import register_security  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _client(cfg: ClientConfig) -> ApiClient:
    return ApiClient(cfg.server_url, cfg.api_key)


def create_sidecar() -> Flask:
    ensure_dirs()
    app = Flask(__name__)
    # 模板上传是唯一的大文件入口，给足余量并留个好读的报错
    app.config["MAX_CONTENT_LENGTH"] = MAX_TEMPLATE_BYTES * 3
    executor = Executor()

    # 晚绑定 shim：路由在**请求时**才经这里解析 _client（即本模块全局）。
    # 测试在 create_sidecar() 之后 monkeypatch.setattr(server, "_client", ...)，
    # 若创建时直接把函数对象传下去，patch 永远打不进路由。
    def _client_lazy(cfg: ClientConfig) -> ApiClient:
        return _client(cfg)

    register_security(app)  # 钩子先挂（保持原注册顺序：413/CORS/跨站写/预检）
    register_status_routes(app, executor, _client_lazy)
    register_template_routes(app)
    register_export_routes(app, executor, _client_lazy)
    register_history_routes(app, _client_lazy)
    register_shell_routes(app, _client_lazy)

    return app


def _attach_file_logger(filename: str) -> None:
    """sidecar 自己的日志落盘（实现与 worker 共用，避免两份格式化逻辑）"""
    from .worker import attach_file_logger

    attach_file_logger(filename)


def main() -> None:
    # PyInstaller 打包后 worker 由同一个 exe 以 --run-worker 方式拉起
    if "--run-worker" in sys.argv:
        idx = sys.argv.index("--run-worker")
        from .worker import run

        run(Path(sys.argv[idx + 1]))
        return

    _attach_file_logger("sidecar.log")
    port = int(os.getenv("SIDECAR_PORT", "8765"))
    app = create_sidecar()
    # 结算不在请求里做完：员工关窗/断线/退出软件时没人来轮询，
    # 上一次跑完的任务就永远报不进服务器。这里先做一次启动对账，
    # 之后每 15 秒兜底扫一遍（两条路都走同一个幂等函数）。
    if settler.start_settler(_client):
        logger.info("任务结算已启动：启动对账 + 每 %.0fs 兜底", settler.SETTLE_INTERVAL_S)
    logger.info("sidecar 启动 http://127.0.0.1:%s", port)

    # 只绑回环：局域网内其他机器不应能调用本机 PS
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
