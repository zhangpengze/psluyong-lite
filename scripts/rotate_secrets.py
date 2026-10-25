"""一把轮换 .env 里的密钥，并把"改完之后还得做什么"直接打出来。

为什么需要它：`.env` 一旦泄露，"删除提交历史"是不够的——force-push 之后
旧提交的 blob 在 GitHub 上仍可被按 SHA 直接下载（2026-10-24 实测 200）。
真正止血只有轮换。手动轮换容易漏三步：① 备份文件又落回仓库里
（`.env.bak.ci` 就是这么被 `git add -A` 带进公开仓库的）；② 忘了改管理员密码
在数据库里其实没变（`models.py::init_seed` 只在查无此人时建号）；
③ 忘了员工客户端要同步。

用法：
    python scripts/rotate_secrets.py                    # 轮换四项，先备份再写
    python scripts/rotate_secrets.py --keys SECRET_KEY  # 只换一项
    python scripts/rotate_secrets.py --dry-run          # 只看计划，不落盘
    python scripts/rotate_secrets.py --no-db            # 跳过数据库密码重置
    python scripts/rotate_secrets.py --yes --reveal     # 无人值守并把新值打到屏幕

容器内（NAS）：
    docker compose exec web python scripts/rotate_secrets.py --yes --backup-dir /tmp/env-backups
    # 或在宿主机改 .env 后：docker compose up -d && docker compose exec web flask reset-password ...

安全约定（脚本自己守）：
  - 备份目录**不允许**落在仓库工作区内，指到里面直接退出（防误提交复发）
  - 默认只打印掩码值，完整新值写进备份目录下的 ROTATED-*.txt
  - 原子写：先写同目录临时文件再 replace，中途失败不会留下半个 .env
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = ROOT / ".env"

# 键 -> 生成时的字节数（ADMIN_PASSWORD 要人手输，短一点；其余给足熵）
#
# 注意 QQ_BOT_SECRET **不在**这里：它不是本地生成的随机串，而是 q.qq.com 后台
# 签发、且只显示一次的凭证。脚本编一个新值写进 .env，机器人立刻全部 401，
# 而脚本还会打印"轮换成功"——这比不轮换更糟。真泄露了要去后台重置 AppSecret
# 再粘回来（DEPLOY.md 有步骤）。
ROTATABLE = {
    "SECRET_KEY": 32,
    "API_KEY": 32,
    "ADMIN_PASSWORD": 12,
}

# 每个键改完之后的连带动作，直接打进收尾清单
SIDE_EFFECTS = {
    "SECRET_KEY": "所有登录 session 失效，后台需重新登录（无其它影响）",
    "API_KEY": "每台员工客户端的 API Key 都要改成新值，否则提交 401",
    "ADMIN_PASSWORD": "数据库里的管理员密码必须一起重置（本脚本默认会做），只改 .env 无效",
}


# ---------- 纯函数：解析与改写 ----------

def parse_pairs(text: str) -> dict[str, str]:
    """取出 KEY=VALUE（忽略注释与空行）。"""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def rotate_text(text: str, keys, generator=None) -> tuple[str, dict[str, str], list[str]]:
    """就地替换指定键的值。

    返回 (新文本, {键: 新值}, 未在 .env 中找到的键)。
    只改目标行：注释、顺序、其它键原样保留——.env 是人读的，不能重排。
    """
    gen = generator or (lambda key: secrets.token_urlsafe(ROTATABLE[key]))
    wanted = set(keys)
    found: dict[str, str] = {}
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in wanted:
                value = gen(key)
                found[key] = value
                out.append(f"{key}={value}")
                continue
        out.append(line)
    missing = [k for k in keys if k not in found]
    tail = "\n" if text.endswith("\n") or out else ""
    return "\n".join(out) + tail, found, missing


def mask(value: str) -> str:
    """只露头尾，够人辨认"确实是新值"即可。"""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}(len={len(value)})"


def write_atomic(path: Path, content: str) -> None:
    """同目录临时文件 + replace，避免写到一半留下残缺 .env。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def check_backup_dir(raw: str | None) -> Path:
    """算出备份目录并做安全检查（不建目录，dry-run 也能提前发现问题）。

    任何落在仓库工作区内的候选一律拒绝——`.env.bak.ci` 就是因为在仓库里
    才被 `git add -A` 顺手带走。
    """
    if raw:
        target = Path(raw)
    elif os.getenv("PSLUYONG_ENV_BACKUP_DIR"):
        target = Path(os.environ["PSLUYONG_ENV_BACKUP_DIR"])
    else:
        target = Path(tempfile.gettempdir()) / "psluyong-env-backups"
    if is_within(target, ROOT):
        raise SystemExit(
            f"[!] 备份目录 {target} 位于仓库工作区内（{ROOT}），已拒绝执行。\n"
            "    密钥备份绝不能进版本库——换个仓库外的目录，例如 "
            f"{Path(tempfile.gettempdir()) / 'psluyong-env-backups'}"
        )
    return target


def resolve_backup_dir(raw: str | None) -> Path:
    target = check_backup_dir(raw)
    target.mkdir(parents=True, exist_ok=True)
    return target


# ---------- 副作用：数据库里的管理员密码 ----------

def reset_admin_password_db(username: str, password: str) -> str:
    """把库里已有管理员的密码改成新值。

    `models.py::init_seed` 只在"查无此人"时按 .env 建号，所以改 .env 对已部署的
    库完全无效——这一步不做，旧密码照样能登。
    """
    try:
        sys.path.insert(0, str(ROOT))
        from server.app import create_app
        from server.app.db import db
    except Exception as exc:  # pragma: no cover - 依赖缺失时走人工兜底
        return f"跳过数据库重置（无法导入 server.app: {exc}）；请手动执行 flask reset-password"

    app = create_app()
    with app.app_context():
        from server.app.models import AdminUser

        user = AdminUser.query.filter_by(username=username).first()
        if user is None:
            return f"库中没有用户 {username}（首次启动会按 .env 的 ADMIN_PASSWORD 建号）"
        user.set_password(password)
        db.session.commit()
    return f"已重置库中 {username} 的密码（旧 session 不影响，登录需用新密码）"


# ---------- CLI ----------

def _parse_keys(raw: str | None) -> list[str]:
    if not raw:
        return list(ROTATABLE)
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    unknown = [k for k in keys if k not in ROTATABLE]
    if unknown:
        raise SystemExit(f"[!] 未知键：{', '.join(unknown)}；可选：{', '.join(ROTATABLE)}")
    return keys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="轮换 .env 密钥并打印连带动作清单")
    ap.add_argument("--env-file", default=str(DEFAULT_ENV), help="默认 <repo>/.env")
    ap.add_argument("--keys", default=None, help="逗号分隔，默认全部三项")
    ap.add_argument("--backup-dir", default=None, help="默认 $PSLUYONG_ENV_BACKUP_DIR 或系统临时目录")
    ap.add_argument("--username", default=None, help="管理员用户名，默认取 ADMIN_USERNAME 或 admin")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件、不碰数据库")
    ap.add_argument("--no-db", action="store_true", help="跳过数据库管理员密码重置")
    ap.add_argument("--yes", action="store_true", help="跳过确认（无人值守）")
    ap.add_argument("--reveal", action="store_true", help="把新值打到屏幕（默认只打掩码）")
    args = ap.parse_args(argv)

    env_file = Path(args.env_file).expanduser()
    if not env_file.exists():
        print(f"[!] 找不到 {env_file}", file=sys.stderr)
        return 1

    keys = _parse_keys(args.keys)
    text = env_file.read_text(encoding="utf-8")
    existing = parse_pairs(text)
    missing = [k for k in keys if k not in existing]
    present = [k for k in keys if k in existing]

    print(f"配置文件：{env_file}")
    print(f"待轮换 {len(present)} 项：{', '.join(present) or '(无)'}")
    for key in missing:
        print(f"[!] .env 里没有 {key}，跳过（缺失的键请对照 .env.example 手工补）")

    if args.dry_run:
        # 备份目录的问题在 dry-run 时就暴露，别等到真跑一半才退出
        print(f"备份目录（校验通过）：{check_backup_dir(args.backup_dir)}")
        for key in present:
            print(f"  {key}: {mask(existing[key])} -> <新随机值>")
        print("dry-run：未写入任何文件")
        return 0

    if not present:
        print("[!] 没有可轮换的键，退出")
        return 1

    if not args.yes:
        if not sys.stdin.isatty():
            print("[!] 非交互环境，请显式加 --yes", file=sys.stderr)
            return 2
        answer = input(f"将改写 {env_file} 并轮换 {len(present)} 个值，输入 yes 继续：").strip().lower()
        if answer != "yes":
            print("已取消")
            return 1

    backup_dir = resolve_backup_dir(args.backup_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_env = backup_dir / f"env-backup-{stamp}"
    backup_env.write_text(text, encoding="utf-8")
    print(f"原文件已备份：{backup_env}")

    new_text, new_values, _ = rotate_text(text, present)
    write_atomic(env_file, new_text)
    print(f"已写回 {env_file}（{len(new_values)} 项）")

    record = backup_dir / f"ROTATED-{stamp}.txt"
    record.write_text(
        "\n".join(f"{k}={v}" for k, v in new_values.items()) + "\n", encoding="utf-8"
    )
    print(f"新值已留档：{record}")

    print("\n新值（掩码）：")
    for key, value in new_values.items():
        shown = value if args.reveal else mask(value)
        print(f"  {key} = {shown}")

    if "ADMIN_PASSWORD" in new_values:
        username = args.username or existing.get("ADMIN_USERNAME") or "admin"
        if args.no_db:
            print("\n[!] 已按 --no-db 跳过数据库密码重置；别忘了："
                  "\n    docker compose exec web flask reset-password --username "
                  f"{username} --password '<新密码>'")
        else:
            print(f"\n管理员密码：{reset_admin_password_db(username, new_values['ADMIN_PASSWORD'])}")

    print("\n还要同步的地方（不同步就会出现 401）：")
    for key in new_values:
        print(f"  - {key}: {SIDE_EFFECTS[key]}")
    print("  - 服务端：容器要重启才读到新 .env（docker compose up -d）")
    print("  - 自检：curl -fsS http://127.0.0.1:8000/health/ready 与一次客户端提交")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
