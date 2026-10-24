"""SQLite 热备、校验、恢复与备份清理（核心存储侧，无 CLI 依赖）。

为什么不用"直接拷贝 app.db"：SQLite 是分页存储，服务运行中拷贝
很可能拿到不一致的页。这里用 sqlite3.Connection.backup() 在线热备，
再 gzip 落盘。文件名带 秒级时间戳 + PID + 随机串——历史上精确到秒
的名字在多进程同秒时会互相覆盖临时文件，教训见旧项目 server/backup.py。

用法与参数解析在 cli.py（`python -m server.backup`）。
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 这两张表缺任何一张都说明备份不完整或库损坏，不能拿来恢复
REQUIRED_TABLES = ("admin_users", "records")

# 备份锁的平台后端：Windows 用 msvcrt，POSIX（NAS 上的 Docker/Linux）用 fcntl。
# 缺任何一个平台分支，那个平台上的定时备份都会静默失败——NAS 部署踩过这个坑。
if sys.platform == "win32":
    import msvcrt  # type: ignore
else:
    import fcntl  # type: ignore


def db_file_path() -> Path:
    """从 SQLAlchemy URI 里取 SQLite 文件路径"""
    from server.app.config import Config

    uri = Config.SQLALCHEMY_DATABASE_URI
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        raise RuntimeError(f"备份只支持 SQLite，当前数据库类型: {uri.split(':')[0]}")
    return Path(uri[len(prefix):])


@contextmanager
def _exclusive_lock(lock_path: Path, timeout_s: int = 30):
    """跨进程互斥：防止定时备份和手动备份同时写。

    平台差异：Windows 用 msvcrt.locking，POSIX（NAS Docker/Linux）用
    fcntl.flock。两者都是"持锁方被杀后锁由 OS 自动释放"，所以循环里每次
    重新抢锁而不是抢不到就放弃——备份不会因为上次持锁方被 kill 而静默停摆。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    acquired = False
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if sys.platform == "win32":
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("另一个备份正在进行，请稍后再试") from None
                time.sleep(0.5)
        yield
    finally:
        if acquired:
            try:
                if sys.platform == "win32":
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def create_backup(db_path: Path, backup_dir: Path, keep: int) -> Path:
    """热备 + gzip 落盘，返回备份文件路径。失败时不留半截文件。"""
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # PID + 随机串防止同秒多进程互踩（历史 bug 的直接教训）
    final_path = backup_dir / f"{db_path.stem}-{stamp}-{os.getpid()}{uuid.uuid4().hex[:4]}.db.gz"
    tmp_path = final_path.with_suffix(".gz.tmp")

    with _exclusive_lock(backup_dir / "backup.lock"):
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            dst_raw = sqlite3.connect(tmp_path)
            try:
                src.backup(dst_raw)  # 在线热备，页级一致
            finally:
                dst_raw.close()
        finally:
            src.close()

        try:
            with open(tmp_path, "rb") as raw, gzip.open(final_path, "wb", compresslevel=6) as gz:
                shutil.copyfileobj(raw, gz, length=1024 * 1024)
            # 备份必须能过完整性校验才算成功，否则等于留了个坏库当"保险"
            _verify_archive(final_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    _cleanup_old(backup_dir, keep)
    logger.info("备份完成 %s (%.1f KB)", final_path.name, final_path.stat().st_size / 1024)
    return final_path


def _verify_archive(archive: Path) -> None:
    db_path = _inflate(archive, Path(archive.with_suffix(".verify.tmp")))
    try:
        _check_db(db_path)
    finally:
        db_path.unlink(missing_ok=True)


def _inflate(archive: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(archive, "rb") as gz, open(target, "wb") as out:
        shutil.copyfileobj(gz, out, length=1024 * 1024)
    return target


def _check_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"数据库完整性校验失败: {result[0] if result else '无结果'}")
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise RuntimeError(f"备份缺少核心表: {', '.join(missing)}")
    finally:
        conn.close()


def _cleanup_old(backup_dir: Path, keep: int) -> None:
    """保留最近 keep 份。

    按 mtime 而不是文件名排序：文件名里带随机串，同一秒内的多次备份
    按名字排序不等于按时间排序，可能误删刚生成的这份（真实踩过的坑）。
    """
    archives = sorted(backup_dir.glob("*.db.gz"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for stale in archives[keep:]:
        stale.unlink(missing_ok=True)
        logger.info("清理过期备份 %s", stale.name)


def list_backups(backup_dir: Path) -> list[Path]:
    return sorted(backup_dir.glob("*.db.gz"), reverse=True)


def restore_backup(archive: Path, db_path: Path, rehearse: bool = False) -> Path:
    """把备份恢复为当前库。彩排模式只校验，不写任何东西。"""
    if not archive.exists():
        raise FileNotFoundError(f"备份文件不存在: {archive}")

    workdir = archive.parent / ".restore-work"
    workdir.mkdir(parents=True, exist_ok=True)
    inflated = _inflate(archive, workdir / "restore.db")
    try:
        _check_db(inflated)
        conn = sqlite3.connect(inflated)
        try:
            for table in REQUIRED_TABLES:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                logger.info("  %s: %s 行", table, count)
        finally:
            conn.close()

        if rehearse:
            logger.info("彩排通过：%s 可安全恢复（未改动现有数据库）", archive.name)
            return inflated

        if not db_path.exists():
            raise FileNotFoundError(f"目标数据库不存在: {db_path}")

        # 恢复前把当前库留档，恢复失败可手工退回
        pre = db_path.with_name(f"{db_path.name}.pre-restore-{uuid.uuid4().hex[:8]}")
        shutil.copy2(db_path, pre)
        logger.info("当前库已留档 %s", pre.name)

        # WAL/SHM 是旧库的残留页，留着会污染新库
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        os.replace(inflated, db_path)
        logger.info("恢复完成：%s -> %s", archive.name, db_path)
        return db_path
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
