"""数据库备份 / 恢复测试（不依赖 Flask app，直接操作临时 SQLite 文件）"""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import pytest

from server.backup import (
    REQUIRED_TABLES,
    create_backup,
    list_backups,
    restore_backup,
)

_SCHEMA = "\n".join(f"CREATE TABLE {t} (id TEXT);" for t in REQUIRED_TABLES)


def _make_db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        for i in range(rows):
            conn.execute("INSERT INTO records (id) VALUES (?)", (f"r{i}",))
        conn.commit()
    finally:
        conn.close()


def _table_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture()
def env(tmp_path: Path):
    src = tmp_path / "data" / "app.db"
    backups = tmp_path / "data" / "backups"
    _make_db(src)
    return src, backups


def test_create_backup_and_roundtrip(env, tmp_path: Path):
    src, backups = env
    archive = create_backup(src, backups, keep=10)
    assert archive.exists() and archive.suffix == ".gz"

    target = tmp_path / "restored" / "app.db"
    target.parent.mkdir(parents=True)
    target.touch()  # 恢复要求目标库已存在
    restore_backup(archive, target)

    assert _table_count(target, "records") == 3
    # 目标库里应包含全部核心表
    conn = sqlite3.connect(target)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert set(REQUIRED_TABLES) <= tables


def test_backup_keeps_recent_n(env):
    src, backups = env
    for _ in range(3):
        create_backup(src, backups, keep=2)
    assert len(list_backups(backups)) == 2


def test_rehearse_does_not_touch_target(env, tmp_path: Path):
    src, backups = env
    archive = create_backup(src, backups, keep=1)

    target = tmp_path / "app.db"
    target.write_bytes(b"original")  # 非法 sqlite，若真被覆盖后续会炸
    restore_backup(archive, target, rehearse=True)
    assert target.read_bytes() == b"original"


def test_restore_rejects_corrupt_archive(env, tmp_path: Path):
    src, backups = env
    bad = backups / "bad.db.gz"
    backups.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"this is not gzip")

    target = tmp_path / "app.db"
    _make_db(target, rows=1)
    with pytest.raises(Exception):
        restore_backup(bad, target)
    # 原库必须原封不动
    assert _table_count(target, "records") == 1


def test_verify_rejects_db_missing_core_tables(env, tmp_path: Path):
    """缺核心表的备份必须在校验时拦下——留个坏库当保险等于没有保险"""
    src, backups = env
    incomplete = tmp_path / "incomplete.db"
    conn = sqlite3.connect(incomplete)
    try:
        conn.execute("CREATE TABLE records (id TEXT)")
        conn.commit()
    finally:
        conn.close()

    # 直接 gzip 一个不完整的库，绕过 create_backup 的校验，验证恢复路径会拦
    backups.mkdir(parents=True, exist_ok=True)
    archives = backups / "incomplete.db.gz"
    with open(incomplete, "rb") as raw, gzip.open(archives, "wb") as gz:
        gz.write(raw.read())

    target = tmp_path / "target.db"
    _make_db(target)
    with pytest.raises(RuntimeError, match="核心表"):
        restore_backup(archives, target)


# ==================== 成品图镜像 ====================


def test_mirror_images_copies_once_then_skips(tmp_path):
    """首次全量复制，第二次只补新增；已存在的同大小文件不重拷"""
    from server.backup import mirror_images

    src = tmp_path / "images"
    dst = tmp_path / "mirror"
    (src / "2026" / "10" / "23").mkdir(parents=True)
    (src / "2026" / "10" / "23" / "a.jpg").write_bytes(b"x" * 100)
    (src / "2026" / "10" / "23" / "b.jpg").write_bytes(b"y" * 50)

    first = mirror_images(src, dst)
    assert first["copied"] == 2 and first["failed"] == 0
    assert (dst / "2026" / "10" / "23" / "a.jpg").read_bytes() == b"x" * 100

    # 再跑一次：全部跳过，不重复写
    second = mirror_images(src, dst)
    assert second["copied"] == 0 and second["skipped"] == 2

    # 新增一张：只补这一张
    (src / "2026" / "10" / "23" / "c.jpg").write_bytes(b"z" * 10)
    third = mirror_images(src, dst)
    assert third["copied"] == 1
    assert (dst / "2026" / "10" / "23" / "c.jpg").exists()


def test_mirror_images_overwrites_changed_size(tmp_path):
    """同文件名但大小变了（重出图）要覆盖，不能因为"目标已存在"就跳过"""
    from server.backup import mirror_images

    src = tmp_path / "images"
    dst = tmp_path / "mirror"
    src.mkdir()
    (src / "a.jpg").write_bytes(b"1" * 10)
    mirror_images(src, dst)

    (src / "a.jpg").write_bytes(b"2" * 99)
    result = mirror_images(src, dst)
    assert result["copied"] == 1
    assert (dst / "a.jpg").read_bytes() == b"2" * 99


def test_mirror_images_missing_source_raises(tmp_path):
    from server.backup import mirror_images

    with pytest.raises(FileNotFoundError):
        mirror_images(tmp_path / "nope", tmp_path / "mirror")

