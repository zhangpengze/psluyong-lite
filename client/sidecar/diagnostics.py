"""一键诊断包：把排障真正需要的东西打成一个 zip。

背景：「复制诊断信息」只有 7 行文本。遇到 P0-5 那种"配置全空、界面全 500"的
场景，员工能给你的就只有这 7 行，而真正有用的东西（sidecar.log 的最后几十行、
最近几个任务的 status.json、队列里积压了什么、服务器 readiness 返回了什么）
全在本地硬盘上，员工不知道去哪找、也不该被要求去找。

包里放什么，按"运维拿到能不能独立判断"来取舍：

    meta.json                版本/系统/PS 识别结果/队列与结算计数
    config.sanitized.json    配置，**api_key 已抹掉**
    logs/*.log               尾部若干行（不打包全量，免得几百 MB）
    executions/<id>/*.json   最近 5 个任务的 params/status/result
    server_health.json       服务端 /health/ready 的真实返回
    README.txt               每个文件是什么、哪些已脱敏

刻意不打包成品图：一张就几 MB，且和排障无关。
"""

from __future__ import annotations

import json
import logging
import platform
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import app_data_dir, last_recovery, load_config
from .ps_info import _resolve_ps

from . import net
logger = logging.getLogger(__name__)

# 日志尾部行数：一次失败的前因后果基本都在最后 ~120 行里
LOG_TAIL_LINES = 300
# 最近几个任务目录：够覆盖"刚才那次"和"反复失败的那几次"
RECENT_TASKS = 5
# 诊断包自身的大小上限（防止日志异常膨胀把 UI 卡死）
MAX_BUNDLE_BYTES = 8 * 1024 * 1024

_README = """期刊录用助手 · 诊断包
========================

这个压缩包是给主管/运维排查问题用的，可以直接发给他们。

文件说明
--------
meta.json               客户端版本、系统信息、Photoshop 识别结果、队列与待补报计数
config.sanitized.json   本机配置。**API Key 已抹掉**，其余（服务器地址、目录、姓名）保留
logs/                   本地服务与生成进程的日志尾部；crash-*.log 是崩溃时留下的
executions/             最近几次生成任务的过程文件：params=请求参数、status=生成结果、
                        result=是否已结算（有它说明服务器或离线队列已收下）
server_health.json      服务端 /health/ready 的返回（它是判断"服务器到底行不行"的第一手证据）
README.txt              本说明

没有打包的内容
--------------
成品图片（体积大且与排障无关）、模板 PSD、以及 API Key。
"""


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    """取文件尾部若干行；读不到就返回说明文字，不让整个过程失败。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"[读取失败 {type(e).__name__}: {e}]"
    rows = text.splitlines()
    if len(rows) <= lines:
        return text
    return f"[仅保留最后 {lines} 行，共 {len(rows)} 行]\n" + "\n".join(rows[-lines:])


def sanitized_config() -> dict:
    """配置里只有 API Key 是必须抹掉的：它是身份凭据，泄漏等于别人能冒充员工。"""
    data = load_config().to_dict()
    key = data.get("api_key") or ""
    data["api_key"] = f"***已隐藏（原长度 {len(key)}）***" if key else ""
    return data


def _queue_counts() -> dict:
    from . import queue

    try:
        return {
            "pending": len(queue.pending()),
            "dead": len(queue.dead_letters()),
            "blocked": len(queue.blocked_items()),
        }
    except Exception as e:  # noqa: BLE001 - 队列目录异常不该让诊断包整体失败
        logger.warning("读取队列计数失败: %s", e)
        return {"error": type(e).__name__}


def _unsynced_count() -> int | str:
    from . import settler

    try:
        return len(settler.unsettled_task_ids(min_age_s=settler.UNSYNCED_MIN_AGE_S))
    except Exception as e:  # noqa: BLE001
        return f"读取失败: {type(e).__name__}"


def _server_health(client) -> dict:
    """探一次服务端 readiness。没有 client（未配置）时如实说明，不编造。"""
    if client is None:
        return {"skipped": "未配置服务器地址或 API Key"}
    url = f"{client.base_url.rstrip('/')}/health/ready"
    try:
        import requests

        resp = net.get(url, timeout=5)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text[:500]}
        return {"url": url, "status_code": resp.status_code, **payload}
    except Exception as e:  # noqa: BLE001 - 探不到本身就是有用信息
        return {"url": url, "error": f"{type(e).__name__}: {e}"}


def _meta(client) -> dict:
    cfg = load_config()
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sidecar_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.node(),
        "employee": cfg.employee_name,
        "server_url": cfg.server_url,
        "queue": _queue_counts(),
        "unsynced": _unsynced_count(),
        "config_recovered": last_recovery(),
        "ps": _resolve_ps(cfg).to_dict(),
    }


def _recent_task_dirs(root: Path, limit: int = RECENT_TASKS) -> list[Path]:
    if not root.is_dir():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.name, reverse=True)  # 目录名是时间戳+随机后缀
    return dirs[:limit]


def _write_json(zf: zipfile.ZipFile, name: str, payload) -> None:
    zf.writestr(name, json.dumps(payload, ensure_ascii=False, indent=2))


def build_bundle(client=None, out_dir: Path | None = None) -> Path:
    """生成诊断包并返回路径。任何单份材料失败都只影响那一份。"""
    root = app_data_dir()
    target = Path(out_dir or tempfile.mkdtemp(prefix="psluyong-diag-"))
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle = target / f"诊断包-{stamp}.zip"

    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", _README)
        _write_json(zf, "meta.json", _meta(client))
        _write_json(zf, "config.sanitized.json", sanitized_config())
        _write_json(zf, "server_health.json", _server_health(client))

        logs = root / "logs"
        if logs.is_dir():
            for log in sorted(logs.glob("*.log")):
                zf.writestr(f"logs/{log.name}", _tail(log))
            for crash in sorted(logs.glob("crash-*.txt"))[-3:]:
                zf.writestr(f"logs/{crash.name}", _tail(crash, 200))

        for task_dir in _recent_task_dirs(root / "executions"):
            for name in ("params.json", "status.json", "result.json"):
                path = task_dir / name
                if path.is_file():
                    zf.writestr(f"executions/{task_dir.name}/{name}", _tail(path, 400))

    size = bundle.stat().st_size
    if size > MAX_BUNDLE_BYTES:
        logger.warning("诊断包偏大（%.1f MB），请检查日志是否异常膨胀", size / 1024 / 1024)
    logger.info("诊断包已生成：%s（%.1f KB）", bundle, size / 1024)
    return bundle
