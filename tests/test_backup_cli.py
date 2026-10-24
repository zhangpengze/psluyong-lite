"""备份 CLI（python -m server.backup）分发与退出码测试

审计缺口：backup/cli.py 只有 24%（只有默认备份分支被间接跑到）。
恢复/镜像/列目录的分发与错误码没有用例——退出码正是定时任务告警的依据，
错了就是「备份坏了没人知道」。配置常量在 main() 内才 import，可安全地指到 tmp。
"""

from __future__ import annotations

import sqlite3

import pytest

from server.backup import REQUIRED_TABLES, create_backup
from server.backup import cli as backup_cli
from server.backup import mirror as mirror_mod


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    """把 BACKUP_DIR / IMAGES_DIR / DB 路径全部指到本用例的 tmp"""
    import server.app.config as config_mod

    data = tmp_path / "data"
    data.mkdir()
    db = data / "app.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript("\n".join(f"CREATE TABLE {t} (id TEXT);" for t in REQUIRED_TABLES))
        conn.execute("INSERT INTO records (id) VALUES ('r1')")
        conn.commit()
    finally:
        conn.close()

    images = tmp_path / "images"
    images.mkdir()
    (images / "a.jpg").write_bytes(b"x" * 10)

    monkeypatch.setattr(config_mod, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config_mod, "IMAGES_DIR", images)
    monkeypatch.setattr(config_mod.Config, "IMAGES_MIRROR_DIR", "")
    monkeypatch.setattr(
        config_mod.Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{db.as_posix()}"
    )
    return {"db": db, "images": images, "backup_dir": tmp_path / "backups",
            "mirror": tmp_path / "mirror", "root": tmp_path}


# ---------- --list ----------

def test_cli_list_with_no_backups_says_so(cli_env, capsys):
    rc = backup_cli.main(["--list"])

    assert rc == 0
    assert "（暂无备份）" in capsys.readouterr().out


def test_cli_list_prints_name_and_size(cli_env, capsys):
    archive = create_backup(cli_env["db"], cli_env["backup_dir"], keep=3)

    rc = backup_cli.main(["--list"])

    out = capsys.readouterr().out
    assert rc == 0
    assert archive.name in out
    assert "KB" in out  # 大小要能一眼看出来，不然没法判断备份是否缩水


# ---------- 默认动作：做一份备份 ----------

def test_cli_default_action_creates_backup(cli_env, capsys):
    rc = backup_cli.main([])

    assert rc == 0
    assert list(cli_env["backup_dir"].glob("*.db.gz")), "不带参数就是做一次备份"


def test_cli_default_action_without_db_returns_error_code(cli_env, capsys):
    """库文件不见了要返回非 0，让定时任务告警，而不是静默成功"""
    cli_env["db"].unlink()

    assert backup_cli.main([]) == 1


# ---------- --restore ----------

def test_cli_restore_missing_archive_returns_error_code(cli_env, capsys):
    rc = backup_cli.main(["--restore", str(cli_env["root"] / "不存在.db.gz")])

    assert rc == 1


def test_cli_restore_rehearse_valid_archive(cli_env, capsys):
    archive = create_backup(cli_env["db"], cli_env["backup_dir"], keep=3)
    before = cli_env["db"].read_bytes()

    rc = backup_cli.main(["--restore", str(archive), "--rehearse"])

    assert rc == 0
    assert cli_env["db"].read_bytes() == before  # 彩排不动当前库


# ---------- --mirror-images ----------

def test_cli_mirror_without_target_or_env_returns_error_code(cli_env, caplog):
    """没配镜像目录必须报错退出，不能悄悄什么都不做还返回 0"""
    assert backup_cli.main(["--mirror-images"]) == 1
    assert "未配置镜像目录" in caplog.text


def test_cli_mirror_images_success_returns_zero(cli_env, capsys):
    rc = backup_cli.main(["--mirror-images", str(cli_env["mirror"])])

    assert rc == 0
    assert (cli_env["mirror"] / "a.jpg").read_bytes() == b"x" * 10


def test_cli_mirror_images_any_copy_failure_returns_one(cli_env, capsys):
    """单张图拷失败也要返回 1——定时任务靠退出码触发告警"""
    dst = cli_env["mirror"] / "a.jpg"
    dst.mkdir(parents=True)
    (dst / "占位").write_bytes(b"occupied")  # 同名目录挡住 os.replace

    assert backup_cli.main(["--mirror-images", str(cli_env["mirror"])]) == 1


def test_cli_mirror_missing_source_returns_error_code(cli_env, monkeypatch):
    import server.app.config as config_mod

    monkeypatch.setattr(config_mod, "IMAGES_DIR", cli_env["root"] / "从没建过")

    # 源目录不存在 -> mirror_images 抛 FileNotFoundError -> 统一错误码 1
    assert backup_cli.main(["--mirror-images", str(cli_env["mirror"])]) == 1


# ---------- dir_size_bytes（系统状态里的图片占用告警） ----------

def test_dir_size_bytes_sums_files(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"12345")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.jpg").write_bytes(b"123")

    assert mirror_mod.dir_size_bytes(tmp_path) == 8


def test_dir_size_bytes_on_missing_path_is_zero(tmp_path):
    """走不通就当 0，不能让系统状态页因为一个路径炸掉"""
    assert mirror_mod.dir_size_bytes(tmp_path / "从没建过") == 0
