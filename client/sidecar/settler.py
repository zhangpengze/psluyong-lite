"""任务结算：把跑完的生成任务落成「服务器记录」或「离线队列」

为什么不再由 /export/status 顺手上报（2026-10-24 修）：
    上报是这个接口的副作用，意味着**只有前端还在轮询时记录才会进服务器**。
    员工点完「生成录用图」之后只要发生下面任何一件事，这条记录就永久消失：
      - 生成还在跑，他把软件退了（Tauri 退出会 kill sidecar）
      - 前端组件卸载 / fetch 抛错，轮询链断掉（busy 一解除就再也不问）
      - 切到别的窗口，页面被销毁
    图躺在输出目录里，后台一条记录都没有，月底结算时才发现少了一批，
    而这时已经查不出是谁、做的哪篇。

现在的分工（三条路都走同一个幂等函数）：
    - 员工还在看着：/export/status 同步结算，成功文案照旧立刻回给前端
    - 前端没人轮询：后台线程每 15 秒扫一遍 executions/，替他把账结了
    - 软件被关掉：下次启动立刻扫一遍（启动对账），上一轮的账先补齐

"结算"的定义：记录进了服务器，**或者**进了离线队列（queue/ 里有完整字段 +
图片路径）。两者都算"不会丢"，都写 result.json 当已结算标记；两个都失败了
才不写，留给下一轮重试。result.json 同时是 /export/status 的响应缓存。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .config import app_data_dir, load_config
from .ps_info import _resolve_ps
from .reporter import record_fields, submit_fields
from .worker import read_json, write_json_atomic

logger = logging.getLogger(__name__)

# 后台兜底扫描间隔：前端不轮询时靠它把账结掉
SETTLE_INTERVAL_S = 15.0
# 只有「结束超过这么久还没结算」才算待补报，避免把刚跑完、正在结算的算进去
UNSYNCED_MIN_AGE_S = 60.0

_CLAIM_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()
_START_LOCK = threading.Lock()
_STARTED = False


# ========== 结算 ==========
def _build_payload(
    cfg: ClientConfig, task_dir: Path, task_id: str, status: dict, client_factory
) -> dict:
    """worker 终态 -> 前端负载（同时是 result.json 的内容）"""
    state = status.get("state")

    if state == "cancelled":
        # 员工自己点的取消，不是失败：绝不能上报一条 failed 记录。
        # 报了的话"今日失败数"里会多出一条查不出原因的记录，成功率
        # 被无端拉低，而真正的原因是"他不想等了"。
        payload = {"ok": False, "cancelled": True, "error": status.get("message", "已取消")}
    elif state == "uncertain":
        payload = {"ok": False, "error": status.get("message", "结果未知"), "uncertain": True}
    elif state == "error" or not status.get("success"):
        # 异常退出（worker 抛错）和"跑完了但没成功"都要入库：
        # 前者以前没人报，客户端整体坏掉时后台一片安静
        error = status.get("message") or ("生成模块异常" if state == "error" else "生成失败")
        fields = record_fields(cfg, task_dir, status, error)
        payload = submit_fields(cfg, fields, "", success=False, client_factory=client_factory)
        payload["error"] = error
        payload["warnings"] = status.get("warnings", [])
    else:
        fields = record_fields(cfg, task_dir, status, "")
        # 用识别出的路径取版本，而不是只认设置里手填的那个：
        # 手填为空时以前这里永远写不出 ps_version，报障时查不到是哪一版
        fields["ps_version"] = _resolve_ps(cfg).version
        payload = submit_fields(
            cfg, fields, status.get("image_path", ""), success=True, client_factory=client_factory
        )
        payload["warnings"] = status.get("warnings", [])
        payload["duration_ms"] = status.get("duration_ms", 0)

    payload["task_id"] = task_id
    payload["finished"] = True
    return payload


def _unsettled_payload(task_dir: Path, status: dict, task_id: str) -> dict:
    """结算失败（磁盘满 / 队列写不进去）时的负载。

    不写 result.json：图片还在，下一轮还要再试一次。但员工必须知道
    "图出来了、记录没存住"，否则他会以为一切正常。
    """
    image = Path(status.get("image_path") or "")
    return {
        "ok": False,
        "error": (
            "图片已生成，但这次记录没能存下来（本地磁盘或服务异常）。"
            f"图片在：{image}，请把这条提示截图给管理员补录。"
        ),
        "unsettled": True,
        "image_path": str(image),
        "image_name": image.name if image.name else "",
        "warnings": status.get("warnings", []),
        "duration_ms": status.get("duration_ms", 0),
        "task_id": task_id,
        "finished": True,
    }


def _claim(key: str) -> bool:
    """同一任务只允许一个线程在结算（前端轮询与后台线程会同时看到它）

    占用权按**目录**记而不是任务 id：任务 id 只是时间戳+随机后缀，
    不同数据目录下重名时不认这个键就会互相卡死（真实踩过：测试里
    上一次运行的残留线程把新任务的 id 占住了，新任务永远结算不了）。
    """
    with _CLAIM_LOCK:
        if key in _INFLIGHT:
            return False
        _INFLIGHT.add(key)
        return True


def _unclaim(key: str) -> None:
    with _CLAIM_LOCK:
        _INFLIGHT.discard(key)


def settle_task(task_id: str, client_factory, status: dict | None = None, cfg=None):
    """结算一个任务（幂等）。

    返回：已结算 -> 结果负载（读缓存或本次刚写）
          还没跑完 -> None
          别的线程正在结算 -> None
          worker 结束但记录没落地 -> 带 unsettled=True 的负载（不缓存）
    """
    task_dir = app_data_dir() / "executions" / task_id
    cached = read_json(task_dir / "result.json")
    if cached is not None:
        return cached
    if status is None:
        status = read_json(task_dir / "status.json")
    if status is None:
        return None
    if not _claim(str(task_dir)):
        return None
    try:
        # 抢到占用权后再看一眼：等锁期间别的线程可能刚结算完
        cached = read_json(task_dir / "result.json")
        if cached is not None:
            return cached
        cfg = cfg or load_config()
        try:
            payload = _build_payload(cfg, task_dir, task_id, status, client_factory)
        except Exception:  # noqa: BLE001 - 记录没落地就绝不能标已结算
            logger.exception("任务结算失败，下一轮重试 task=%s", task_id)
            return _unsettled_payload(task_dir, status, task_id)
        write_json_atomic(task_dir / "result.json", payload)
        logger.info(
            "任务已结算 task=%s state=%s uploaded=%s queued=%s",
            task_id,
            status.get("state"),
            payload.get("uploaded"),
            payload.get("queued"),
        )
        return payload
    finally:
        _unclaim(str(task_dir))


def unsettled_task_ids(min_age_s: float = 0.0) -> list[str]:
    """跑完但还没结算的任务（status.json 有、result.json 没有）"""
    root = app_data_dir() / "executions"
    if not root.is_dir():
        return []
    now = time.time()
    out: list[str] = []
    for task_dir in sorted(root.iterdir()):
        if not task_dir.is_dir():
            continue
        if (task_dir / "result.json").exists():
            continue
        status_path = task_dir / "status.json"
        if not status_path.exists():
            continue  # 还在跑
        try:
            # 负数归零：Windows 上文件 mtime 偶尔比 time.time() 大几微秒，
            # 不夹住的话"刚跑完"的任务会被判成"太新"从而永远不进清单
            age = max(0.0, now - status_path.stat().st_mtime)
        except OSError:
            continue
        if age < min_age_s:
            continue
        out.append(task_dir.name)
    return out


def settle_pending(client_factory, cfg=None, min_age_s: float = 0.0) -> dict:
    """扫一遍 executions/，把没结算的补上"""
    task_ids = unsettled_task_ids(min_age_s)
    settled = failed = 0
    if task_ids:
        cfg = cfg or load_config()
    for task_id in task_ids:
        payload = settle_task(task_id, client_factory, cfg=cfg)
        if payload is None:
            continue  # 另一个线程抢先了
        if payload.get("unsettled"):
            failed += 1
        else:
            settled += 1
    return {"settled": settled, "failed": failed, "remaining": len(unsettled_task_ids())}


def _loop(client_factory, interval: float, stop: threading.Event | None) -> None:
    while True:
        # 停机用 Event 而不是 sleep：退出软件时不必等满一个周期
        if stop is not None:
            if stop.wait(interval):
                return
        else:
            time.sleep(interval)
        try:
            settle_pending(client_factory)
        except Exception:  # noqa: BLE001 - 后台线程崩了没人会发现，绝不能裸奔
            logger.warning("后台结算异常", exc_info=True)


def start_settler(
    client_factory, interval: float = SETTLE_INTERVAL_S, stop: threading.Event | None = None
) -> bool:
    """启动后台结算（只启动一次）：立刻做启动对账，之后每 interval 秒兜底一次。

    启动对账必须放到后台：服务器连不上时每条记录要等超时，几十条积压会把
    启动卡住几十秒——前端那时只会看到"本地服务连接失败"。
    """
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return False
        _STARTED = True

    def first_pass() -> None:
        try:
            result = settle_pending(client_factory)
        except Exception:  # noqa: BLE001
            logger.warning("启动对账异常", exc_info=True)
            return
        if result["settled"] or result["failed"]:
            logger.info(
                "启动对账：补结算 %d 个任务，%d 个待重试，仍有 %d 个未结算",
                result["settled"],
                result["failed"],
                result["remaining"],
            )

    threading.Thread(target=first_pass, name="settler-reconcile", daemon=True).start()
    threading.Thread(
        target=_loop,
        args=(client_factory, interval, stop),
        name="settler-loop",
        daemon=True,
    ).start()
    return True
