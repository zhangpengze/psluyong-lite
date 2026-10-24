"""系统探针取数：数据库连通性 + 数据目录可写 + 调度器存活。

供 routes/health.py 使用。纯参数进、纯结果出——异常在函数内部收敛成
"某项失败了"，由路由层决定响应码（失败细节只写日志，不回给调用方）。

为什么只查 SELECT 1 不够：磁盘塞满 / 数据卷被挂成只读时，读操作照样成功，
写入却全部 500。而 compose 的 healthcheck 用的正是这个端点——探针说"就绪"、
实际一条记录都存不进去，是最难查的那种故障。
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import text

from ..db import db

logger = logging.getLogger(__name__)

# 数据目录探测用的临时文件前缀：看得出来是谁写的，出问题好找
_PROBE_PREFIX = ".readiness-probe-"

# 就绪判定里的"致命项"：这两项失败就是真的不能用，必须 503
CRITICAL = ("db", "data_dir")


def ping_database() -> None:
    """真连一次数据库（SELECT 1）。连不上抛异常，由调用方映射 503。"""
    db.session.execute(text("SELECT 1"))


def probe_data_dir() -> str:
    """真写一个文件再删掉。写不了就抛 OSError。

    只做 os.access(W_OK) 不够：只读挂载、配额用尽、inode 耗尽都能通过
    权限检查但写不进去。这里走完整的 open/write/fsync/close 链路。
    """
    from ..config import DATA_DIR

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / f"{_PROBE_PREFIX}{os.getpid()}.tmp"
    try:
        with open(target, "wb") as fh:
            fh.write(b"ok")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        try:
            target.unlink()
        except OSError:
            pass


def scheduler_state() -> tuple[str, str]:
    """调度器状态：(state, detail)，state ∈ ok / off / unknown。

    刻意不把"本进程没跑调度器"算成就绪失败：多进程部署时只有持有调度器
    的那个进程该有 job，其他进程没有才是正常的。真正要抓的是
    "说自己在跑，却一个 job 都没有"——那才是启动流程坏了。
    """
    from ..config import Config

    if not Config.ENABLE_SCHEDULER:
        return "off", "ENABLE_SCHEDULER=0，日报由外部触发"

    from bot.scheduler import scheduler_status

    info = scheduler_status()
    if not info.get("running"):
        return "off", "本进程未启动调度器"
    jobs = info.get("jobs", 0)
    if jobs <= 0:
        logger.error("调度器在运行但没有任何 job，日报将不会被推送")
        return "error", "调度器在运行但没有任何 job"
    detail = f"{jobs} 个 job"
    if info.get("next_run"):
        detail += f"，下次 {info['next_run']}"
    return "ok", detail


def collect_readiness() -> dict:
    """跑全部探针，返回可直接 jsonify 的结构。

    形状对老调用方向后兼容（ok / status / db 三个键还在），新增
    checks 明细供排障——但失败原因只记日志，不给调用方（不泄露路径与 SQL）。
    """
    checks: dict[str, dict] = {}

    for name, probe in (("db", ping_database), ("data_dir", probe_data_dir)):
        try:
            probe()
            checks[name] = {"state": "ok", "detail": ""}
        except Exception as e:  # noqa: BLE001 - 探针失败就是失败，不区分异常类型
            logger.exception("就绪探针失败（%s）: %s", name, e)
            checks[name] = {"state": "error", "detail": type(e).__name__}

    try:
        state, detail = scheduler_state()
    except Exception as e:  # noqa: BLE001 - 调度器信息取不到不该拖垮探针
        logger.exception("调度器状态读取失败: %s", e)
        state, detail = "unknown", type(e).__name__
    checks["scheduler"] = {"state": state, "detail": detail}

    ready = all(checks[name]["state"] == "ok" for name in CRITICAL)
    return {
        "ok": ready,
        "status": "ready" if ready else "not_ready",
        "db": checks["db"]["state"],
        "checks": checks,
    }
