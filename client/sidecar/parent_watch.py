"""父进程看门狗：父进程一死，自己跟着退

Windows 上父进程被任务管理器强杀、或升级安装把它替换掉时，子进程**收不到任何
通知**。旧版没有这层看门狗，于是：

1. Tauri 被强杀 → sidecar 继续跑、继续占着 8765。下次启动的新 sidecar 绑不上
   端口，异常栈进了 `Stdio::null()`（打包后 stdout/stderr 就是黑洞），静默退出；
   前端照样能连上 8765 —— 连到的是那个**僵尸**。它手里的 busy、配置、队列全是
   上一轮的，于是"软件启动了但按钮一直说忙"、"改了设置没反应"，日志里一个字
   都没有。
2. Tauri 正常退出 → Rust 只 `kill()` 一级子进程，worker 是孙进程。sidecar 一死
   worker 就成孤儿，还抱着 ps.lock 和 PS 的 COM，下一次生成要等 10 秒抢锁失败，
   报"Photoshop 正被另一个任务占用"。

所以两边各挂一条：**worker 盯 sidecar，sidecar 盯 Tauri**。pid 由父进程通过环境
变量传下来（`PSLUYONG_PARENT_PID`）；没传就不启用——手动 `python -m
client.sidecar.server` 或跑测试时本来就没有父进程要盯。

退出用 `os._exit`：此刻 COM 那句调用可能正卡着，atexit / logging 收尾都可能
永远等不来，而我们就差这一口气。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

PARENT_PID_ENV = "PSLUYONG_PARENT_PID"
# 2 秒一次足够快：僵尸多活 2 秒不会有人察觉，而查一次进程只是个 API 调用
CHECK_INTERVAL_S = 2.0


def pid_from_env() -> int | None:
    """读父进程 pid。没配/配坏都返回 None（不启用），绝不因此让进程起不来。"""
    raw = (os.getenv(PARENT_PID_ENV) or "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        logger.warning("%s 不是数字（%r），不启用父进程看门狗", PARENT_PID_ENV, raw)
        return None
    return pid if pid > 0 else None


def alive(pid: int) -> bool:
    """进程还在不在。

    **Windows 上不能用 `os.kill(pid, 0)`**：那里除 CTRL_C/CTRL_BREAK 之外的任何
    sig 都会直接 TerminateProcess —— sig=0 也一样，等于把父进程杀了。所以
    Windows 走 OpenProcess + WaitForSingleObject。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _alive_win(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 存在但没权限查（不是我们的子进程）：当成活着，宁可不动
        return True
    except OSError:
        return False
    return True


def _alive_win(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x0010_0000
    QUERY_LIMITED = 0x1000
    WAIT_TIMEOUT = 0x102

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE | QUERY_LIMITED, False, wintypes.DWORD(pid))
    if not handle:
        # 打不开 = 进程没了（或权限不足；两者都只能当作"没了"，
        # 因为这种情况下我们也做不了别的）
        return False
    try:
        # 已经退出的进程句柄是"已信号"状态，等 0 毫秒就能立刻判出来
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def start(
    on_lost=None,
    interval: float = CHECK_INTERVAL_S,
    pid: int | None = None,
    exit_fn=os._exit,
) -> threading.Thread | None:
    """起一条守护线程盯父进程。没配 pid 返回 None。

    on_lost：退出前想做的事（比如先把子进程收掉）。
    exit_fn：默认 `os._exit`，测试里换成一个记录函数——真跳出去会把 pytest 一起杀了。
    """
    target_pid = pid if pid is not None else pid_from_env()
    if target_pid is None:
        return None

    def watch() -> None:
        while True:
            time.sleep(interval)
            if alive(target_pid):
                continue
            logger.warning(
                "父进程 %s 已退出，本进程跟着退出（否则会占着端口变成僵尸）", target_pid
            )
            if on_lost is not None:
                try:
                    on_lost()
                except Exception:  # noqa: BLE001 - 收尾失败也必须退
                    logger.exception("父进程退出后的收尾动作失败")
            exit_fn(0)
            return

    thread = threading.Thread(target=watch, name="parent-watch", daemon=True)
    thread.start()
    return thread
