"""COM 执行 worker：独立子进程里驱动 Photoshop

为什么非要一个子进程：PS 的 COM 组件遇到访问违例是 Python 捕获不到的
硬崩溃，会直接带走整个进程。COM 跑在 sidecar 主进程里，一次崩溃 =
客户端全部功能瘫痪；跑在 worker 里，崩溃只损失这一次任务，sidecar
还能正常报"结果未知"并接受下一次任务。

父子进程约定（全部原子写，防撕裂）：
    <task_dir>/params.json     父 -> 子：任务参数
    <task_dir>/status.json     子 -> 父：终态（done/error）
    <task_dir>/heartbeat       子进程存活心跳（每 2 秒 touch）
    <task_dir>/crash.log       faulthandler 段错误现场

ps.lock 进程锁：Windows 下 Photoshop 的 COM 对象同一时刻只能被一个
进程可靠驱动。锁由 OS 在进程死亡时自动释放，不存在"陈旧锁"问题。
"""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 2.0


def write_json_atomic(path: Path, payload: dict) -> None:
    """先写临时文件再 os.replace，读方永远不会看到半截 JSON"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _acquire_ps_lock(lock_path: Path, timeout_s: float = 10.0) -> object:
    """跨进程互斥锁。返回持有句柄（供释放），拿不到抛 RuntimeError。"""
    import msvcrt  # type: ignore  # 本客户端只跑 Windows

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return fh
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                raise RuntimeError(
                    "Photoshop 正被另一个任务占用，请等它结束后再试"
                ) from None
            time.sleep(0.3)


def _release_ps_lock(fh) -> None:
    import msvcrt  # type: ignore

    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass  # 进程退出也会释放，这里尽力而为
    finally:
        fh.close()


def attach_file_logger(filename: str, max_bytes: int = 2 * 1024 * 1024, backups: int = 3) -> None:
    """把日志同时写进 %APPDATA%/psluyong-lite/logs/。

    打包后 sidecar / worker 的 stdout、stderr 都被 Tauri 丢进 DEVNULL
    （员工不该看到黑框），不落文件的话，员工报"生成失败"时手里一份日志都没有。
    """
    from logging.handlers import RotatingFileHandler

    from .config import app_data_dir

    try:
        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / filename, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        logging.getLogger().addHandler(handler)
        logging.getLogger(__name__).info("日志已落盘 %s", log_dir / filename)
    except OSError as e:  # 磁盘满/无权限都不该挡住主流程
        logging.getLogger(__name__).warning("日志文件不可写，仅输出到控制台: %s", e)


def run(task_dir: Path) -> None:
    """worker 主流程：读参数 -> 抢锁 -> 驱动 PS -> 写终态"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [worker] %(message)s",
    )
    attach_file_logger("worker.log")
    task_dir = Path(task_dir)
    # PS 硬崩溃时 Python 拦不到，faulthandler 把现场写进文件，事后有据可查
    crash_log = open(task_dir / "crash.log", "w", encoding="utf-8")
    faulthandler.enable(crash_log)

    params = read_json(task_dir / "params.json")
    if params is None:
        write_json_atomic(task_dir / "status.json", {"state": "error", "message": "任务参数缺失或损坏"})
        return

    stop_heartbeat = threading.Event()

    def beat() -> None:
        hb = task_dir / "heartbeat"
        while not stop_heartbeat.wait(HEARTBEAT_INTERVAL_S):
            try:
                hb.touch()
            except OSError:
                pass

    threading.Thread(target=beat, name="heartbeat", daemon=True).start()

    lock_fh = None
    try:
        from .config import app_data_dir

        lock_fh = _acquire_ps_lock(app_data_dir() / "ps.lock")

        from core import ExportRequest, run_export

        result = run_export(
            ExportRequest(
                template_path=Path(params["template_path"]),
                author=params["author"],
                title=params["title"],
                output_dir=Path(params["output_dir"]),
                output_format=params.get("output_format", "JPG"),
                jpg_quality=int(params.get("jpg_quality", 10)),
                author_placeholder=params.get("author_placeholder") or "{{作者}}",
                title_placeholder=params.get("title_placeholder") or "{{标题}}",
                ps_path=params.get("ps_path", ""),
                filename_rule=params.get("filename_rule") or "author_title",
            )
        )
        write_json_atomic(
            task_dir / "status.json",
            {
                "state": "done",
                "success": result.success,
                "image_path": str(result.image_path) if result.image_path else "",
                "message": result.message,
                "warnings": result.warnings,
                "duration_ms": result.duration_ms,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 终态必须落盘，任何异常都不能静默
        logger.exception("worker 执行失败")
        write_json_atomic(
            task_dir / "status.json",
            {"state": "error", "message": f"生成模块异常: {exc}"},
        )
    finally:
        if lock_fh is not None:
            _release_ps_lock(lock_fh)
        stop_heartbeat.set()
        crash_log.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python -m client.sidecar.worker <task_dir>", file=sys.stderr)
        return 2
    # 保证能 import 到仓库根的 core/ 与 client/（开发与测试环境）
    repo_root = Path(__file__).resolve().parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    run(Path(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
