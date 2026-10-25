"""scripts/rotate_secrets.py 的测试。

这个脚本是"泄露止血"的操作件，跑错一次就把生产 .env 写坏或把密钥备份塞回仓库里，
所以三类风险都钉死：
  - 改写只动目标行（注释/顺序/其它键原样），缺键要报出来而不是凭空补
  - 备份目录落在仓库工作区内必须拒绝（.env.bak.ci 事故的直接复现路径）
  - dry-run 真的不落盘；真跑一次后 .env 值变了、备份与留档都在、目录里无残留临时文件
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("rotate_secrets", REPO / "scripts" / "rotate_secrets.py")
assert SPEC and SPEC.loader
rs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rs)


SAMPLE = """# 服务端配置（注释不能丢）
SECRET_KEY=old-secret
API_KEY=old-api-key

# 下面是部署项，不该被动
TZ=Asia/Shanghai
ADMIN_USERNAME=admin
ADMIN_PASSWORD=old-password
"""


def test_rotate_only_touches_target_keys():
    text, found, missing = rs.rotate_text(
        SAMPLE, ["SECRET_KEY", "ADMIN_PASSWORD"], generator=lambda k: f"new-{k.lower()}"
    )
    lines = text.splitlines()
    assert "SECRET_KEY=new-secret_key" in lines
    assert "ADMIN_PASSWORD=new-admin_password" in lines
    # 注释、空行、无关键原样保留，且相对顺序不变
    assert lines[0] == "# 服务端配置（注释不能丢）"
    assert "# 下面是部署项，不该被动" in lines
    assert "API_KEY=old-api-key" in lines
    assert "TZ=Asia/Shanghai" in lines
    assert "ADMIN_USERNAME=admin" in lines
    assert lines.index("API_KEY=old-api-key") < lines.index("TZ=Asia/Shanghai")
    assert missing == []
    assert set(found) == {"SECRET_KEY", "ADMIN_PASSWORD"}


def test_rotate_reports_missing_key_instead_of_inventing():
    """文件里没有的键：报出来，但绝不能凭空追加一行"""
    text, found, missing = rs.rotate_text(SAMPLE, ["SECRET_KEY", "NOT_IN_FILE"], generator=lambda k: "x")
    assert missing == ["NOT_IN_FILE"]
    assert "NOT_IN_FILE" not in found
    assert "NOT_IN_FILE" not in text


def test_rotate_preserves_trailing_newline():
    text, _, _ = rs.rotate_text(SAMPLE, ["SECRET_KEY"], generator=lambda k: "v")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_mask_hides_middle():
    assert rs.mask("abcdefghijklmnop") == "abcd...mnop(len=16)"
    assert rs.mask("short") == "*****"


def test_backup_dir_inside_repo_is_rejected(tmp_path):
    with pytest.raises(SystemExit) as exc:
        rs.resolve_backup_dir(str(REPO / "env-backups"))
    assert "仓库工作区" in str(exc.value)


def test_backup_dir_outside_repo_is_created(tmp_path):
    target = tmp_path / "bk"
    assert rs.resolve_backup_dir(str(target)) == target
    assert target.is_dir()


def test_write_atomic_leaves_no_temp_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n", encoding="utf-8")
    rs.write_atomic(p, "A=2\n")
    assert p.read_text(encoding="utf-8") == "A=2\n"
    assert [f.name for f in tmp_path.iterdir()] == [".env"]


def test_dry_run_does_not_write(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text(SAMPLE, encoding="utf-8")
    code = rs.main(["--env-file", str(env), "--dry-run"])
    assert code == 0
    assert env.read_text(encoding="utf-8") == SAMPLE
    out = capsys.readouterr().out
    assert "dry-run" in out
    # 掩码可见头尾，完整旧值不该出现在输出里
    assert "old-secret" not in out


def test_rotation_end_to_end(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text(SAMPLE, encoding="utf-8")
    bk = tmp_path / "backups"

    code = rs.main([
        "--env-file", str(env),
        "--keys", "SECRET_KEY,ADMIN_PASSWORD",
        "--backup-dir", str(bk),
        "--yes", "--no-db",
    ])
    assert code == 0

    after = rs.parse_pairs(env.read_text(encoding="utf-8"))
    assert after["SECRET_KEY"] != "old-secret"
    assert after["ADMIN_PASSWORD"] != "old-password"
    assert after["TZ"] == "Asia/Shanghai"          # 无关键不动
    assert after["API_KEY"] == "old-api-key"       # 未指定就不换
    assert "NOT_IN_FILE" not in after              # 原文件没有的键永不凭空出现

    names = sorted(f.name for f in bk.iterdir())
    assert any(n.startswith("env-backup-") for n in names)
    assert any(n.startswith("ROTATED-") for n in names)
    # 备份里是旧值，留档里是新值
    backup = next(bk.glob("env-backup-*")).read_text(encoding="utf-8")
    assert "SECRET_KEY=old-secret" in backup
    record = next(bk.glob("ROTATED-*")).read_text(encoding="utf-8")
    assert "SECRET_KEY=" in record and "old-secret" not in record

    out = capsys.readouterr().out
    assert "--no-db" in out or "跳过数据库密码重置" in out


def test_dry_run_rejects_in_repo_backup_dir(tmp_path):
    """dry-run 也要提前拦住仓库内的备份目录——真跑到一半才退出最坑。"""
    env = tmp_path / ".env"
    env.write_text(SAMPLE, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        rs.main(["--env-file", str(env), "--dry-run", "--backup-dir", str(REPO / "env-backups")])
    assert "仓库工作区" in str(exc.value)


def test_unknown_key_is_rejected(tmp_path):
    env = tmp_path / ".env"
    env.write_text(SAMPLE, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        rs.main(["--env-file", str(env), "--keys", "SECRET_KEY,NOT_A_KEY", "--dry-run"])
    assert "未知键" in str(exc.value)
