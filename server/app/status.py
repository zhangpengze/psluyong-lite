"""系统自身状态：服务 / 数据库 / 备份 / 日报推送 / 图片存储。

从 web/views.py 拆出来共享：仪表盘页面和 /api/v1/stats/overview 都要用，
放在 views 里会让 routes 包反向依赖 web 包。

业务指标再好看，备份挂了、日报没推、磁盘塞满也是白搭——
这几行以前只能 SSH 上去看，现在仪表盘底部一张卡一眼可见。
每行一个组件：状态点（绿/琥珀/红/灰）+ 组件名 · 状态 + 右侧真实指标。

刻意不造假指标：没有网关和集群可探测，就不编"API 网关 23 ms"这种数——
右侧放的是能真查到的东西（记录数、库大小、备份时间、磁盘占用）。
状态分档口径集中在下面的常量里，页面底部不再重复解释。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# 进程启动时刻：服务行的"已运行多久"（waitress 单进程多线程，import 即启动）
_STARTED_AT = time.monotonic()

_CACHE: dict = {"at": 0.0, "ttl": 0.0, "data": None}
# waitress 8 个线程会同时打这个接口（仪表盘 + /stats/overview），
# 边读边写 _CACHE 会读到 at 更新了但 data 还是旧的那一半
_CACHE_LOCK = threading.Lock()
_TTL = 30.0
_TTL_EMPTY = 5.0

# 备份超过 36 小时没备上就该看（每天一备）
_BACKUP_STALE_H = 36
# 磁盘占用达到阈值 80% 开始预警
_DISK_WARN_RATIO = 0.8

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_BAD = "bad"
STATUS_OFF = "off"  # 未配置 / 不适用

# 组件 key → 展示名（顺序即页面顺序）
SYS_COMPONENTS = [
    ("service", "后台服务"),
    ("database", "数据库"),
    ("backup", "数据库备份"),
    ("report", "日报推送"),
    ("images", "图片存储"),
]


def _filesize(value) -> str:
    if not value:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _human_age(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))} 分钟前"
    if hours < 24:
        return f"{int(hours)} 小时前"
    return f"{int(hours // 24)} 天前"


def _uptime_text() -> str:
    seconds = int(time.monotonic() - _STARTED_AT)
    if seconds < 60:
        return "刚启动"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"已运行 {days} 天 {hours} 小时"
    if hours:
        return f"已运行 {hours} 小时 {minutes} 分"
    return f"已运行 {minutes} 分钟"


def _db_file() -> Path | None:
    from .config import Config

    uri = Config.SQLALCHEMY_DATABASE_URI
    prefix = "sqlite:///"
    if uri.startswith(prefix):
        return Path(uri[len(prefix):])
    return None


def _service_row() -> dict:
    # 页面能渲染出来，服务就是活的——这行不是摆设，是给"重启后多久了"一个答案
    from bot.single_instance import describe

    uptime = _uptime_text()
    single, note = describe()
    if single:
        return {"status": STATUS_OK, "detail": uptime}
    # 多副本：页面还能用，但日报会重复推——黄点提醒，别用红点当事故吓人
    return {"status": STATUS_WARN, "detail": f"{uptime} · {note}"}


def _database_row() -> dict:
    from .models import Record

    records = Record.query.filter(Record.is_deleted.is_(False)).count() or 0
    size_text = "-"
    db_file = _db_file()
    if db_file and db_file.exists():
        try:
            size_text = _filesize(db_file.stat().st_size)
        except OSError:
            size_text = "读不到"
    return {"status": STATUS_OK, "detail": f"{records:,} 条 · {size_text}"}


def _backup_row() -> dict:
    from .config import BACKUP_DIR

    backups = sorted(BACKUP_DIR.glob("*.db.gz"), reverse=True) if BACKUP_DIR.exists() else []
    if not backups:
        return {
            "status": STATUS_BAD,
            "detail": "还没有数据库备份，建议先跑一次 python -m server.backup",
            "backups": 0,
        }
    latest = backups[0]
    try:
        mtime = datetime.fromtimestamp(latest.stat().st_mtime)
    except OSError:
        return {"status": STATUS_WARN, "detail": "备份文件时间读不到，请检查磁盘", "backups": len(backups)}
    age_h = (datetime.now() - mtime).total_seconds() / 3600
    return {
        "status": STATUS_WARN if age_h > _BACKUP_STALE_H else STATUS_OK,
        "detail": f"最后备份 {_human_age(age_h)}（{mtime:%m-%d %H:%M}）· 共 {len(backups)} 份",
        "backups": len(backups),
    }


def _due_day(now: datetime | None = None) -> date:
    """当下"该已经推送成功"的那一天。

    旧实现只看昨天：18:00 没推成功，要等到第二天才变黄点，整整 24 小时没人知道。
    现在按配置的推送时点算——过了今天的推送时点就查今天，否则查昨天。
    """
    from .config import Config

    now = now or datetime.now()
    due_today = (now.hour, now.minute) >= (Config.DAILY_REPORT_HOUR, Config.DAILY_REPORT_MINUTE)
    return now.date() if due_today else now.date() - timedelta(days=1)


def _report_row() -> dict:
    from .config import DATA_DIR

    from bot.qqbot_sender import config_problem

    # "没配"和"配了但推失败"必须分开显示：前者是灰点（管理员还没接机器人），
    # 后者是红点（接了却推不出去，这才是事故）。只看推送状态文件的话，
    # 没配推送的部署会永远显示"日报没有推送记录"，像是天天出故障。
    problem = config_problem(need_group=True)
    if problem:
        return {"status": STATUS_OFF, "detail": f"推送通道未配置（{problem}），日报只生成不推送"}
    day = _due_day()
    status_file = DATA_DIR / "reports" / f"{day:%Y-%m-%d}.status.json"
    if not status_file.exists():
        return {"status": STATUS_WARN, "detail": f"{day} 日报没有推送记录"}
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": STATUS_WARN, "detail": f"{day} 日报推送状态读不到"}
    if payload.get("pushed"):
        suffix = "（已出终版）" if payload.get("final") else ""
        return {"status": STATUS_OK, "detail": f"{day} 日报已推送{suffix}"}
    message = (payload.get("message") or "").strip()[:80]
    return {
        "status": STATUS_BAD,
        "detail": f"{day} 日报推送失败" + (f" · {message}" if message else ""),
    }


def _images_row() -> dict:
    from .config import Config, IMAGES_DIR
    from .dir_usage import dir_usage

    count, size_bytes = dir_usage(IMAGES_DIR)
    threshold = Config.DISK_ALERT_GB
    limit_bytes = threshold * 1024**3 if threshold else 0
    if limit_bytes and size_bytes >= limit_bytes:
        status = STATUS_BAD
    elif limit_bytes and size_bytes >= limit_bytes * _DISK_WARN_RATIO:
        status = STATUS_WARN
    else:
        status = STATUS_OK
    detail = f"图片 {count:,} 张 · {_filesize(size_bytes)}"
    if threshold:
        detail += f"（阈值 {threshold:g} GB）"
    return {"status": status, "detail": detail, "count": count}


def _safe_row(name: str, builder) -> dict:
    """取数失败的单行降级成红色行，不牵连整张卡。

    这张卡和 /api/v1/stats/overview 共用同一份数据：以前 _database_row 里的
    `Record.query.count()` 没有 try/except（同文件的 _images_row / _backup_row
    都做了降级），WAL 下高峰期一次锁冲突就让整张卡连同概览接口一起 500——
    而"系统到底好不好"恰恰是最该在故障时能打开的那一页。
    """
    try:
        return builder()
    except Exception as e:  # noqa: BLE001 - 单行取数失败不能拖垮整卡
        logger.warning("系统状态行取数失败（%s）: %s", name, e, exc_info=True)
        return {
            "status": STATUS_BAD,
            "detail": f"取数失败 · {type(e).__name__}",
            "error": str(e)[:200],
        }


def system_status() -> dict:
    """仪表盘底部那张"系统自己好不好"。

    目录遍历有成本，30 秒内复用同一份结果；数据还空着时只缓存 5 秒——
    刚灌完第一批记录就刷新页面，不该看到"图片 0 张"这种明显过期的数字。
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE["data"] is not None and now - _CACHE["at"] < _CACHE["ttl"]:
            return _CACHE["data"]

    rows = {
        "service": _safe_row("service", _service_row),
        "database": _safe_row("database", _database_row),
        "backup": _safe_row("backup", _backup_row),
        "report": _safe_row("report", _report_row),
        "images": _safe_row("images", _images_row),
    }
    has_data = rows["images"].get("count") or rows["backup"].get("backups")
    data = {"rows": rows}
    with _CACHE_LOCK:
        _CACHE.update(at=now, ttl=_TTL if has_data else _TTL_EMPTY, data=data)
    return data
