"""打一个可直接丢到 NAS 上部署的 zip 包。

产物内容（zip 根目录即 NAS 上的项目目录）：
    Dockerfile            构建服务端镜像
    requirements.txt
    server/ bot/ core/    源码
    docker-compose.yml    NAS 专用编排（extra_hosts 指向宿主机）
    .env                  沿用本机密钥 + 已配好的 NapCat 设置
                          （--no-env 出厂包不含 .env，改放 .env.example）
    data/                 运行时数据（可用 --no-data 排除）
    0-操作步骤.txt        包内操作说明

用法：
    .venv\\Scripts\\python.exe scripts\\package_nas.py            # 含现有数据
    .venv\\Scripts\\python.exe scripts\\package_nas.py --no-data  # 空库部署
    .venv\\Scripts\\python.exe scripts\\package_nas.py --no-env   # 出厂包：不含 .env 密钥

默认把本机 `.env`（SECRET_KEY / API_KEY / ADMIN_PASSWORD 等）打进 zip——**属敏感件，
只在受控内网流转**。发给其它机器/同事用 `--no-env` 出厂包：zip 里改为放
`.env.example`，目标机上自建 `.env`。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = Path.home() / "Desktop"

COPY_DIRS = ["server", "bot", "core"]
COPY_FILES = ["requirements.txt"]
# NAS 上用精简版 Dockerfile（不跑 apt-get，pip 走清华源），避免国内构建卡死
DOCKERFILE_SRC = "deploy/nas/Dockerfile.lean"
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIX = {".pyc", ".pyo"}

README = """# psluyong-lite 服务端（NAS 部署版）

## 一、在 NAS 上跑起来

1. **上传**：UGOS「文件管理」→ docker 共享文件夹 → 新建目录 `psluyong-lite` →
   把本压缩包里的所有内容上传进去并**解压**（保证 `docker-compose.yml` 在该目录第一层）。
2. **建项目**：UGOS「Docker」→「项目」→「创建」→ 项目目录选 `psluyong-lite` →
   编排文件选 `docker-compose.yml` → 创建并启动。
   首次会构建镜像（拉 python:3.11-slim + pip 装依赖），大约 3~8 分钟。
3. **验证**：容器状态为「运行中」后，浏览器打开 `http://NAS的IP:8080/health/ready`，
   返回 ok 即成功。

## 二、日报推送（官方 QQ 机器人）

`.env` 里这三项必须填对，否则日报只生成不推送：

    QQ_BOT_APPID=<q.qq.com 后台的 AppID>
    QQ_BOT_SECRET=<q.qq.com 后台的 AppSecret>
    QQ_BOT_GROUP_OPENID=<群的 openid，不是群号>

拿 `QQ_BOT_GROUP_OPENID`：服务端没有入站端点（@ 机器人不会进服务端日志），
只能在开发机上跑 `python scripts/qqbot_spike.py` 自动抓——脚本不进 NAS 镜像。
跑之前把开发机的出口公网 IP 加进机器人 IP 白名单（拿到 openid 后可删）。

**必须做的两件事**（漏了就会一直 40034102 主动消息无权限）：
1. 手机 QQ 打开该群 → 设置 → 机器人 → 打开「机器人主动在群聊内发言」；
2. q.qq.com 后台把 NAS 的出口公网 IP 加进机器人 IP 白名单。

验证推送：后台「每日日报」→ 选一天 → 点「重推此日报」，徽标变绿即可。

> 老版本用 NapCat（登录一个真 QQ 号）推群，已整体移除。升级时把 `.env` 里的
> `NAPCAT_WEBHOOK` / `NAPCAT_TOKEN` / `REPORT_GROUP_ID` 三行删掉，同时把 napcat
> 容器停掉——它存的是一个真 QQ 的登录态，留着就是个风险点。

## 三、数据

- 所有数据都在 `data/`（app.db、images、reports、backups），备份它就是备份全部。
- 换机迁移：把 `data/` 整个拷到新机器的项目目录即可。
- 若 `data/` 放在 NFS/SMB 网络共享上，需在 `.env` 加 `SQLITE_JOURNAL_MODE=DELETE`
  （WAL 需要本地文件锁）。

## 四、登录

`.env` 里的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 与原来一致。
默认 `http://NAS的IP:8080/`，登录后进「每日日报」。

## 五、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 40034102 主动消息失败, 无权限 | 群里没开「机器人主动在群聊内发言」 | 手机 QQ 群设置 → 机器人 → 打开该开关 |
| 401 / 40013 | AppID/AppSecret 填错或已重置 | 到 q.qq.com 复制新值改 `.env`，重启项目 |
| 40054002 group_openid 无效 | 填成了群号 | 按上面第二节的方法取真正的 openid |
| 11244 频率限制 | 群主动消息 60 条/分钟、1000 条/天 | 等一分钟；超了就是有人在刷重推 |
| 「该机器人去火星了」 | 出口 IP 不在白名单 | q.qq.com 后台补 IP 白名单 |
| 员工客户端登录失败 | `API_KEY` 被改过 | `API_KEY` 必须与客户端里填的一致 |
"""


def _env_parse(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    return data


def _render_env(local: dict[str, str]) -> str:
    """沿用本机密钥（API_KEY 必须一致，否则员工客户端要重配），只调整部署相关项。"""
    def get(key: str, default: str = "") -> str:
        return local.get(key, default)

    lines = [
        "# ===== 由 scripts/package_nas.py 生成 =====",
        "# API_KEY 必须与员工客户端里填的一致，改了客户端就连不上",
        f"SECRET_KEY={get('SECRET_KEY')}",
        f"API_KEY={get('API_KEY')}",
        f"ADMIN_USERNAME={get('ADMIN_USERNAME', 'admin')}",
        f"ADMIN_PASSWORD={get('ADMIN_PASSWORD')}",
        "",
        "# ===== 日报推送 =====",
        "ENABLE_SCHEDULER=1",
        "DAILY_REPORT_HOUR=18",
        "DAILY_REPORT_MINUTE=0",
        "# 官方机器人：AppID/AppSecret 在 q.qq.com 后台，openid 从群消息事件里取",
        f"QQ_BOT_APPID={get('QQ_BOT_APPID')}",
        f"QQ_BOT_SECRET={get('QQ_BOT_SECRET')}",
        f"QQ_BOT_GROUP_OPENID={get('QQ_BOT_GROUP_OPENID')}",
        "",
        "# ===== 其他 =====",
        "TZ=Asia/Shanghai",
        "MAX_UPLOAD_MB=64",
        "FORCE_HTTPS=0",
    ]
    return "\n".join(lines) + "\n"


def _copy_tree(src: Path, dst: Path) -> int:
    """拷贝目录，跳过缓存与运行时数据。返回文件数。"""
    count = 0
    for item in src.rglob("*"):
        if not item.is_file():
            continue
        if SKIP_PARTS & set(item.parts):
            continue
        if item.suffix in SKIP_SUFFIX:
            continue
        rel = item.relative_to(src)
        if rel.parts and rel.parts[0] == "data":
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def _copy_data(staging: Path) -> str:
    """用 sqlite 在线备份拷库，保证一致性；再带上图片与日报。"""
    local_data = ROOT / "server" / "data"
    if not local_data.exists():
        return "无本地数据，跳过"

    dst = staging / "data"
    dst.mkdir(parents=True, exist_ok=True)

    src_db = local_data / "app.db"
    if src_db.exists():
        with sqlite3.connect(f"file:{src_db}?mode=ro", uri=True) as s, \
                sqlite3.connect(dst / "app.db") as d:
            s.backup(d)
        size = (dst / "app.db").stat().st_size / 1024 / 1024
        db_note = f"app.db {size:.1f}MB"
    else:
        db_note = "app.db 不存在"

    for sub in ("images", "reports", "backups"):
        s = local_data / sub
        if s.is_dir():
            shutil.copytree(s, dst / sub, dirs_exist_ok=True)
    return db_note


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-data", action="store_true", help="不打包 data/，部署空库")
    ap.add_argument("--no-env", action="store_true",
                    help="不把 .env 打进 zip（出厂包：包内放 .env.example，密钥在目标机现填）")
    ap.add_argument("--code-only", action="store_true",
                    help="只打包代码与构建文件（不含 .env / data），用于更新已有部署")
    ap.add_argument("--out", type=Path, help="输出 zip 路径（默认桌面）")
    args = ap.parse_args()

    out = args.out or DESKTOP / f"psluyong-lite-nas-{date.today():%Y%m%d}.zip"
    local_env = _env_parse(ROOT / ".env")

    staging = Path(tempfile.mkdtemp(prefix="pslnas-"))
    try:
        total = 0
        for name in COPY_FILES:
            src = ROOT / name
            if src.exists():
                shutil.copy2(src, staging / name)
                total += 1

        # 精简版 Dockerfile，打包时统一命名为 Dockerfile
        shutil.copy2(ROOT / DOCKERFILE_SRC, staging / "Dockerfile")
        total += 1

        # 构建上下文瘦身：data/ 走 volume，不该进镜像
        shutil.copy2(ROOT / "deploy" / "nas" / ".dockerignore", staging / ".dockerignore")
        total += 1

        # 包内说明书
        shutil.copy2(ROOT / "deploy" / "nas" / "0-操作步骤.txt", staging / "0-操作步骤.txt")
        total += 1

        for name in COPY_DIRS:
            src = ROOT / name
            if src.is_dir():
                total += _copy_tree(src, staging / name)

        # 运维脚本单独带：容器内要能跑密钥轮换（docker compose exec web python scripts/...）。
        # 不整目录拷——probe_*/shot_*/verify_* 这些开发探针没有上生产的理由。
        ops_script = ROOT / "scripts" / "rotate_secrets.py"
        if ops_script.exists():
            (staging / "scripts").mkdir(parents=True, exist_ok=True)
            shutil.copy2(ops_script, staging / "scripts" / "rotate_secrets.py")
            total += 1

        shutil.copy2(ROOT / "deploy" / "nas" / "docker-compose.yml", staging / "docker-compose.yml")
        if args.no_env:
            # 出厂包：不把本机密钥打进 zip，改放模板，目标机上 cp .env.example .env 现填
            shutil.copy2(ROOT / ".env.example", staging / ".env.example")
            total += 1
            env_note = "已排除（--no-env 出厂包，包内为 .env.example）"
        elif not args.code_only:
            (staging / ".env").write_text(_render_env(local_env), encoding="utf-8")
            env_note = "已包含 .env（含密钥，敏感件，勿外发）"
        else:
            env_note = "已排除（--code-only）"

        data_note = "已排除（空库部署）"
        if not args.no_data and not args.code_only:
            data_note = _copy_data(staging)

        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        # 显式写入目录条目：部分 NAS 的解压工具不会自动创建隐式父目录，
        # 只打包文件条目会导致解压后 server/bot/core 这些文件夹整个消失。
        dir_entries = sorted(
            item.relative_to(staging).as_posix() + "/"
            for item in staging.rglob("*")
            if item.is_dir()
        )
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for d in dir_entries:
                z.writestr(zipfile.ZipInfo(d), b"")
            for item in sorted(staging.rglob("*")):
                if item.is_file():
                    z.write(item, item.relative_to(staging).as_posix())
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    size_mb = out.stat().st_size / 1024 / 1024
    print("已生成:", out)
    print(f"  体积      : {size_mb:.1f}MB")
    print(f"  源码文件  : {total} 个")
    print(f"  密钥      : {env_note}")
    print(f"  数据      : {data_note}")
    print()
    print("下一步：UGOS 文件管理 → 上传这个 zip → 解压到 docker/psluyong-lite/")
    print("        → Docker 应用 → 项目 → 创建，选该目录的 docker-compose.yml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
