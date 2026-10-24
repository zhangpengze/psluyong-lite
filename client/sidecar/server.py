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
import socket
import sys
from pathlib import Path

# 允许从 client/sidecar 直接运行：把仓库根加进 sys.path 才能 import core
# （必须先于任何 import core 的子模块执行——routes_* 在下方 import）
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from flask import Flask  # noqa: E402

from . import __version__  # noqa: E402,F401  重新导出：测试与外部读 sidecar_server.__version__
from . import parent_watch, settler  # noqa: E402
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


def probe_port(port: int) -> str:
    """端口能不能用。空串 = 能用，否则是给人看的原因。

    **不能只靠 app.run 抛异常**：Windows 上 werkzeug 会开 SO_REUSEADDR，端口被
    占时 bind 有可能照样成功，于是两个 sidecar 同时"活着"，请求落到谁手里看运气
    —— 比直接失败更难查。所以自己先裸 bind 一次（不带 SO_REUSEADDR），拿不到就
    说明这个端口上已经有人在应答。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as e:
            return f"端口 {port} 已被占用（{e}）——很可能是上一次的 sidecar 还活着，或别的程序占了它"
    return ""


def serve(app: Flask, port: int) -> None:
    """绑定端口开始服务。**端口被占必须留下痕迹。**

    打包后 sidecar 的 stdout/stderr 都是 `Stdio::null()`（员工不该看到黑框），
    所以绑不上时的异常栈只会进黑洞：新 sidecar 悄无声息地死了，前端连上的其实
    是上一次那个僵尸（busy 永远 true、配置是旧的），日志里一个字都没有。
    """
    problem = probe_port(port)
    if problem:
        logger.error("sidecar 启动失败：%s", problem)
        logger.error(
            "处理办法：任务管理器里结束多余的 python/psluyong-sidecar 再启动；"
            "临时换端口可以设 SIDECAR_PORT"
        )
        raise SystemExit(1)

    try:
        # 只绑回环：局域网内其他机器不应能调用本机 PS
        app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
    except OSError as e:
        # 探针到 app.run 之间被抢走（TOCTOU）：同样要留下痕迹
        logger.error("sidecar 启动失败：端口 %s 绑不上（%s）", port, e)
        raise SystemExit(1) from e


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
    # 父进程（Tauri）没了就跟着退：否则本进程会继续占着 8765 变成僵尸，
    # 下次启动的 sidecar 绑不上端口、静默死在 DEVNULL，前端连到的是上一轮的我们
    if parent_watch.start() is not None:
        logger.info("父进程看门狗已启动（pid=%s）", parent_watch.pid_from_env())
    logger.info("sidecar 启动 http://127.0.0.1:%s pid=%s", port, os.getpid())
    serve(app, port)


if __name__ == "__main__":
    main()
