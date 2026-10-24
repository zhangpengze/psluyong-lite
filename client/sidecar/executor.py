"""执行管理器：sidecar 进程内的任务登记与状态查询

职责边界：这里只管"子进程的生老病死"和状态文件的解读，
真正的 COM 调用在 worker 子进程里（见 worker.py）。

状态判定：
    status.json 存在                -> 终态（done/error/timeout）
    进程还在 + 没到硬超时           -> running
    进程还在 + 超过硬超时           -> timeout（本进程主动中止，见 HARD_TIMEOUT_S）
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

from . import parent_watch, task_paths
from .timeout import HARD_TIMEOUT_S, should_abort, timeout_final  # noqa: F401
from .worker import read_json, write_json_atomic

logger = logging.getLogger(__name__)

HEARTBEAT_STALE_S = 15.0
KEEP_TASK_DIRS = 20

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _map_final(final: dict) -> dict:
    """worker 终态文件 -> 对外状态字典（内存路径与磁盘回退路径共用）"""
    return {
        "state": final.get("state", "error"),
        "finished": True,
        "success": final.get("success", False),
        "image_path": final.get("image_path", ""),
        "message": final.get("message", ""),
        "warnings": final.get("warnings", []),
        "duration_ms": final.get("duration_ms", 0),
    }


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
            # 立即初始化心跳文件：worker 的首次 touch 在第 2 秒，不预热的话
            # 前两秒 heartbeat.exists() 为 False，age 按 999s 算 → 客户端
            # 会误报「耗时比平时长」（真实踩坑：刚点生成就弹告警）
            (task_dir / "heartbeat").touch()

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
            # 任务已不在内存（reaper 巡查释放，或上一次轮询已读过终态）。
            # 终态以磁盘 status.json 为准，绝不能报 unknown——那会让上层
            # 落入"未成功"分支报"生成失败"：图已经生出来了、记录却没上报，
            # 员工白干一次。真实 race：worker 退出后 reaper（5s 巡查）
            # 先于客户端轮询（1s 间隔）释放任务，客户端就收到假失败。
            # 路径一律经 task_paths 拼：非法 id 直接当"没有这个任务"，
            # 绝不让请求里的字符串跳到 executions/ 之外。
            from .config import app_data_dir

            fallback_dir = task_paths.task_dir(app_data_dir(), task_id)
            final = read_json(fallback_dir / "status.json") if fallback_dir else None
            if final is not None:
                return _map_final(final)
            return {"state": "unknown", "finished": True}

        popen: subprocess.Popen = task["popen"]
        task_dir: Path = task["dir"]
        final = read_json(task_dir / "status.json")

        if final is not None:
            self._release(task_id)
            return _map_final(final)

        code = popen.poll()
        if code is None:
            elapsed_s = time.monotonic() - task["started"]
            if should_abort(elapsed_s):
                return self._abort_timeout(task_id, task)
            heartbeat = task_dir / "heartbeat"
            age = time.monotonic() - heartbeat.stat().st_mtime if heartbeat.exists() else 999.0
            return {
                "state": "running",
                "finished": False,
                "elapsed_s": int(elapsed_s),
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

    def cancel(self, task_id: str) -> tuple[bool, str]:
        """中止一个正在跑的任务。

        顺序很关键：**先写终态文件，再杀进程**。反过来的话 status() 会先看到
        进程消失、又读不到 status.json，判成 uncertain（"结果未知，请到输出目录
        确认"）——员工自己点的取消，却被告知"可能已经生成了一张图"，然后跑去
        翻文件夹。取消必须是确定的。

        杀进程是唯一手段：worker 此刻阻塞在 COM 调用里，既没有管道也没有
        事件循环，任何 Python 侧的取消标志都读不到。代价是 PS 里会留下一个
        已打开的模板文档（runner 的 finally close_document 跑不到了），
        所以返回文案必须提醒员工手动关掉。
        """
        task = self._tasks.get(task_id)
        if task is None:
            # 已经在别处结束（reaper 释放 / 上前一次轮询已读终态），
            # 不该报错吓人，让前端按"已结束"处理即可
            return False, "任务已经结束了"

        task_dir: Path = task["dir"]
        elapsed_ms = int((time.monotonic() - task["started"]) * 1000)
        write_json_atomic(
            task_dir / "status.json",
            {
                "state": "cancelled",
                "success": False,
                "image_path": "",
                "message": (
                    "已取消生成。Photoshop 里可能留下这个模板的未保存文档，"
                    "请手动关掉再重试，否则文档会越叠越多。"
                ),
                "warnings": [],
                "duration_ms": elapsed_ms,
            },
        )

        popen: subprocess.Popen = task["popen"]
        try:
            popen.kill()
        except OSError as e:
            logger.warning("取消时终止 worker 失败 task=%s: %s", task_id, e)

        logger.info("任务已取消 task=%s 耗时=%sms", task_id, elapsed_ms)
        self._release(task_id)
        return True, "已取消"

    # ---- 内部 ----

    def _abort_timeout(self, task_id: str, task: dict) -> dict:
        """硬超时：写终态 -> 杀 worker -> 放锁。

        顺序和 cancel() 一样不能反：先杀进程的话，status() 会先看到进程消失、
        又读不到 status.json，判成 uncertain（"结果未知"），员工被告知的话术
        就变成了"可能已经生成了一张图"——我们明明知道是超时。
        """
        task_dir: Path = task["dir"]
        elapsed_ms = int((time.monotonic() - task["started"]) * 1000)
        final = timeout_final(elapsed_ms)
        write_json_atomic(task_dir / "status.json", final)
        try:
            task["popen"].kill()
        except OSError as e:
            logger.warning("超时后终止 worker 失败 task=%s: %s", task_id, e)
        logger.error("任务硬超时 task=%s，已跑 %.0f 秒，强制中止", task_id, elapsed_ms / 1000)
        self._release(task_id)
        return _map_final(final)

    def _ensure_reaper(self) -> None:
        """后台线程兜底：客户端关掉没人轮询时，也要把已结束任务占用的锁放掉。

        硬超时也在这里判：员工关了窗口、前端死了，就没人调 status()，
        光靠轮询路径的话 busy 锁会永远挂着。
        """
        if self._reaper_started:
            return
        self._reaper_started = True

        def reap() -> None:
            while True:
                time.sleep(5)
                self._sweep_once()

        threading.Thread(target=reap, name="executor-reaper", daemon=True).start()

    def _sweep_once(self) -> None:
        """巡查一轮：已结束的放锁，跑太久的强制中止。

        单独一个方法是为了能测——"没人轮询时 busy 也要释放"正是这一层要保证的
        事，而它藏在一条 `while True: sleep(5)` 里，不拆出来就只能靠等 5 秒。
        """
        for task_id, task in list(self._tasks.items()):
            popen: subprocess.Popen = task["popen"]
            task_dir: Path = task["dir"]
            if popen.poll() is None:
                if should_abort(time.monotonic() - task["started"]):
                    self._abort_timeout(task_id, task)
                continue
            if read_json(task_dir / "status.json") is not None:
                self._release(task_id)

    def _spawn_worker(self, task_dir: Path) -> subprocess.Popen:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        # worker 盯**我们**：sidecar 一死（正常退出时 Rust 只 kill 一级子进程），
        # worker 就是孤儿，还抱着 ps.lock 和 PS 的 COM，下一次生成要等 10 秒
        # 抢锁失败并报"Photoshop 正被另一个任务占用"。
        # 必须显式覆盖：env 是从 sidecar 继承的，里面那个 pid 是 Tauri 的。
        env[parent_watch.PARENT_PID_ENV] = str(os.getpid())
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
