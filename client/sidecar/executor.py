"""执行管理器：sidecar 进程内的任务登记与状态查询

职责边界：这里只管"子进程的生老病死"和状态文件的解读，
真正的 COM 调用在 worker 子进程里（见 worker.py）。

状态判定：
    status.json 存在                -> 终态（done/error）
    进程还在 + 心跳新鲜             -> running
    进程退了但终态没写出来           -> uncertain（PS 硬崩溃，结果未知）
                                       这时宁可让员工去输出目录看一眼，
                                       也绝不报"成功"——假成功比失败更贵。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .worker import read_json, write_json_atomic

logger = logging.getLogger(__name__)

HEARTBEAT_STALE_S = 15.0
KEEP_TASK_DIRS = 20

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class BusyError(RuntimeError):
    """已有一个生成任务在跑（COM 只能单任务驱动）"""


class Executor:
    def __init__(self) -> None:
        # 非阻塞抢占：忙时直接拒绝，绝不排队——排队会让员工以为点了没反应再点一次
        self._held = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._current_task_id: str | None = None
        self._reaper_started = False

    # ---- 对外 ----

    def start(self, params: dict) -> str:
        if not self._held.acquire(blocking=False):
            raise BusyError("上一次生成还没结束，请等它完成后再点")
        self._ensure_reaper()

        try:
            from .config import app_data_dir

            task_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            task_dir = app_data_dir() / "executions" / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            write_json_atomic(task_dir / "params.json", params)

            popen = self._spawn_worker(task_dir)
            self._tasks[task_id] = {
                "popen": popen,
                "dir": task_dir,
                "started": time.monotonic(),
            }
            self._current_task_id = task_id
            self._cleanup_old_dirs(task_dir.parent)
            logger.info("任务已启动 %s (pid=%s)", task_id, popen.pid)
            return task_id
        except Exception:
            self._held.release()
            raise

    def status(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if task is None:
            return {"state": "unknown", "finished": True}

        popen: subprocess.Popen = task["popen"]
        task_dir: Path = task["dir"]
        final = read_json(task_dir / "status.json")

        if final is not None:
            self._release(task_id)
            return {
                "state": final.get("state", "error"),
                "finished": True,
                "success": final.get("success", False),
                "image_path": final.get("image_path", ""),
                "message": final.get("message", ""),
                "warnings": final.get("warnings", []),
                "duration_ms": final.get("duration_ms", 0),
            }

        code = popen.poll()
        if code is None:
            heartbeat = task_dir / "heartbeat"
            age = time.monotonic() - heartbeat.stat().st_mtime if heartbeat.exists() else 999.0
            return {
                "state": "running",
                "finished": False,
                "elapsed_s": int(time.monotonic() - task["started"]),
                "heartbeat_stale": age > HEARTBEAT_STALE_S,
            }

        # 进程退了却没有终态文件：崩溃（最典型是 PS 访问违例带走 worker）
        self._release(task_id)
        logger.error("worker 异常退出 task=%s exit=%s，结果未知", task_id, code)
        return {
            "state": "uncertain",
            "finished": True,
            "success": False,
            "message": (
                "生成过程异常中断，结果未知。请到输出目录确认图片是否已生成，"
                f"然后重试（退出码 {code}）。"
            ),
            "warnings": [],
        }

    def running_task(self) -> str | None:
        """当前占用的任务 id（给 /status 展示"生成中"状态）"""
        return self._current_task_id

    # ---- 内部 ----

    def _ensure_reaper(self) -> None:
        """后台线程兜底：客户端关掉没人轮询时，也要把已结束任务占用的锁放掉"""
        if self._reaper_started:
            return
        self._reaper_started = True

        def reap() -> None:
            while True:
                time.sleep(5)
                for task_id, task in list(self._tasks.items()):
                    popen: subprocess.Popen = task["popen"]
                    task_dir: Path = task["dir"]
                    if popen.poll() is not None and read_json(task_dir / "status.json") is not None:
                        self._release(task_id)

        threading.Thread(target=reap, name="executor-reaper", daemon=True).start()

    def _spawn_worker(self, task_dir: Path) -> subprocess.Popen:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        if getattr(sys, "frozen", False):
            # PyInstaller 打包后没有可 import 的 client 包，走 exe 自身入口
            cmd = [sys.executable, "--run-worker", str(task_dir)]
        else:
            cmd = [sys.executable, "-m", "client.sidecar.worker", str(task_dir)]
        return subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

    def _release(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)
        if self._current_task_id == task_id:
            self._current_task_id = None
        try:
            self._held.release()
        except RuntimeError:
            pass  # 已释放（uncertain 后又轮询一次等场景）

    def _cleanup_old_dirs(self, root: Path) -> None:
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
        for stale in dirs[KEEP_TASK_DIRS:]:
            try:
                import shutil

                shutil.rmtree(stale, ignore_errors=True)
            except OSError:
                pass
