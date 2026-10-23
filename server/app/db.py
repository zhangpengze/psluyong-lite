"""数据库连接与基类"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()
logger = logging.getLogger(__name__)


@event.listens_for(Engine, "connect")
def _apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ARG001
    """SQLite 连接初始化：WAL + 忙等 + NORMAL 同步。

    为什么必须显式设：
      - 默认 rollback journal 下写事务持排他锁，第二个写入方立刻拿到
        "database is locked"。服务是 waitress 8 线程，员工端下午集中提交时
        create_record 与后台删除完全可能同时发生，偶发 500 就是这么来的。
      - busy_timeout 让撞锁的一方等一会儿而不是立刻失败。
      - synchronous=NORMAL 在 WAL 下是安全的（掉电最多丢最后一个事务，
        不会坏库），写入速度相比 FULL 有数量级差异。

    只在 sqlite3 驱动上执行；其余方言（将来换 MySQL）直接跳过。

    注意：WAL 依赖共享内存（-shm 文件），只适合本地磁盘。如果 data 目录
    其实是挂载的 NFS/SMB 网络共享，WAL 会不可靠——那种部署把
    SQLITE_JOURNAL_MODE 设成 DELETE（见 docs/DEPLOY.md）。
    """
    module = type(dbapi_connection).__module__.split(".")[0]
    if module != "sqlite3":
        return

    mode = os.getenv("SQLITE_JOURNAL_MODE", "WAL").upper()
    if mode not in ("WAL", "DELETE", "TRUNCATE", "PERSIST"):
        mode = "WAL"

    try:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA journal_mode={mode}")
            cursor.fetchone()  # journal_mode 会返回一行结果，不取会留在游标里
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()
    except Exception as e:  # noqa: BLE001 - PRAGMA 失败不该阻止服务启动
        logger.warning("SQLite PRAGMA 设置失败（继续用默认值）: %s", e)


def utcnow() -> datetime:
    """统一用 UTC 存库，展示时再转本地时区，避免夏令时/时区迁移踩坑。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()
