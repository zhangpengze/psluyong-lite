"""上线前自检：把 DEPLOY 第四节的检查清单跑一遍，机器能判的自动判。

「上线检查清单」原先是人肉勾选的——勾的人未必是真跑过，跑过的人下次未必还记得
哪几条。经验是：清单里凡是机器能判的，就该让机器判，人的注意力留给真正需要
判断的那几条（灰度体验、员工培训、备份演练）。

三种结果，含义写清楚，别让人猜：
    PASS  机器验过，通过
    FAIL  机器验过，不通过 —— 退出码 1，可直接进 CI / 上线前置闸门
    TODO  机器判不了（要么服务没起，要么本来就靠人看）—— 需要人勾

用法：
    python scripts/preflight_check.py              # 人类可读表格
    python scripts/preflight_check.py --json       # 给 CI 消费
    python scripts/preflight_check.py --base-url http://127.0.0.1:8080   # 顺带探线上探针
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, FAIL, TODO = "PASS", "FAIL", "TODO"

# 示例值：.env.example 里的占位符照抄进生产环境是最常见的"配了但没配"
_PLACEHOLDER_EXACT = {
    "changeme",
    "change-me",
    "your-secret-key",
    "your-api-key",
    "admin123",
    "password",
    "secret",
    "example",
}

# 这几个词做子串匹配是安全的：16 位以上的真密钥不可能包含它们，
# 而人手写的占位符几乎都会（please-change-me-please / example-key-long-enough）。
# "secret"/"password" 不放进来——真密钥里出现这两个词是可能的，误杀比漏判更伤人。
_PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "change_me",
    "example",
    "placeholder",
    "your-secret",
    "your-api",
    "xxxx",
)


def _looks_like_placeholder(value: str) -> bool:
    v = value.strip().lower()
    if v in _PLACEHOLDER_EXACT:
        return True
    flat = v.replace("-", "").replace("_", "").replace(" ", "")
    return any(
        m.replace("-", "").replace("_", "").replace(" ", "") in flat
        for m in _PLACEHOLDER_MARKERS
    )


def _env_value(key: str) -> str:
    import os

    return (os.getenv(key) or "").strip()


# ---------- 各项检查 ----------

def check_secrets() -> tuple[str, str, str]:
    """三个密钥不能是空值/示例值：空 SECRET_KEY 会让所有会话失效，空 API_KEY 等于不设防"""
    from server.app.config import Config

    bad = []
    for key, value in (
        ("SECRET_KEY", Config.SECRET_KEY),
        ("API_KEY", Config.API_KEY),
        ("ADMIN_PASSWORD", Config.ADMIN_PASSWORD),
    ):
        v = (value or "").strip()
        if not v:
            bad.append(f"{key} 为空")
        elif _looks_like_placeholder(v):
            bad.append(f"{key} 还是示例值")
        elif len(v) < 12:
            bad.append(f"{key} 太短（{len(v)} 字符，建议 ≥16）")
    if bad:
        return FAIL, "；".join(bad), "python scripts/rotate_secrets.py --yes"
    return PASS, "三个密钥均已设置且非示例值", ""


def check_data_dir_writable() -> tuple[str, str, str]:
    """真写一次再删：磁盘满/只读卷时 SELECT 1 照样过，写入却全 500"""
    from server.app.config import DATA_DIR
    from server.app.services.health_service import probe_data_dir

    try:
        probe_data_dir()
    except Exception as e:  # noqa: BLE001
        return FAIL, f"{DATA_DIR} 不可写：{type(e).__name__}: {e}", "检查磁盘空间与挂载权限"
    return PASS, f"{DATA_DIR} 可写", ""


def check_wal_mode() -> tuple[str, str, str]:
    from sqlalchemy import text

    from server.app.db import db

    try:
        mode = db.session.execute(text("PRAGMA journal_mode")).scalar()
    except Exception as e:  # noqa: BLE001
        return FAIL, f"读不到 journal_mode：{type(e).__name__}", "确认数据库文件可访问"
    mode = str(mode or "").lower()
    if mode == "wal":
        return PASS, "journal_mode=wal", ""
    # 数据目录在网络盘上时 WAL 不可靠，DEPLOY 允许改 DELETE
    return TODO, f"journal_mode={mode}", "本地盘应为 wal；网络盘上 DELETE 可接受，人工确认"


def check_backup_dir() -> tuple[str, str, str]:
    from server.app.config import BACKUP_DIR

    if not BACKUP_DIR.exists():
        return FAIL, f"备份目录不存在：{BACKUP_DIR}", "python -m server.backup 跑一次"
    backups = sorted(BACKUP_DIR.glob("*.db.gz"))
    if not backups:
        return FAIL, f"{BACKUP_DIR} 里没有任何 *.db.gz", "python -m server.backup 跑一次"
    return PASS, f"{len(backups)} 份备份，最新 {backups[-1].name}", ""


def check_images_dir() -> tuple[str, str, str]:
    from server.app.config import IMAGES_DIR

    if not IMAGES_DIR.exists():
        return TODO, f"{IMAGES_DIR} 还不存在（首次上传后才创建）", "出过第一张图后再验一次"
    n = sum(1 for p in IMAGES_DIR.rglob("*") if p.is_file())
    return PASS, f"图片目录可访问，{n} 个文件", ""


def check_mirror_dir() -> tuple[str, str, str]:
    """图片第二份：盘坏即全部丢失，这是本项目最大的单点"""
    from server.app.config import Config, IMAGES_DIR

    target = (Config.IMAGES_MIRROR_DIR or "").strip()
    if not target:
        return FAIL, "未配置 IMAGES_MIRROR_DIR（成品图只有一份）", "指向另一块盘或另一台 NAS"
    path = Path(target)
    if not path.exists():
        return FAIL, f"镜像目录不存在：{path}", "先建好目录并确认可写"
    try:
        probe = path / ".preflight-probe"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as e:
        return FAIL, f"镜像目录不可写：{e}", "检查共享盘权限/凭据"
    same_disk = bool(path.drive) and path.drive.lower() == IMAGES_DIR.drive.lower()
    if same_disk:
        return TODO, f"镜像目录与图片目录在同一分区（{path.drive}）", "同一块盘坏了两份一起没，建议换盘"
    return PASS, f"镜像目录可写：{path}", ""


def check_scheduler() -> tuple[str, str, str]:
    """调度器只有跑起来的进程里才有 job；脚本独立运行判不了，交给 readiness 探针"""
    from server.app.config import Config

    if not Config.ENABLE_SCHEDULER:
        return TODO, "ENABLE_SCHEDULER=0，日报需外部触发", "确认外部调度已就位"
    return TODO, "需在运行中的服务里确认（见 --base-url 的 readiness 输出）", ""


def check_readiness(base_url: str) -> tuple[str, str, str]:
    if not base_url:
        return TODO, "未指定 --base-url，跳过线上探针", "加 --base-url http://127.0.0.1:8080"
    url = f"{base_url.rstrip('/')}/health/ready"
    try:
        import requests

        # 绕开系统代理：运维笔记本上常配着 HTTP_PROXY，探自己的服务不该被送去代理，
        # 否则代理回一个 502，会被误读成"服务挂了"（详见 client/sidecar/net.py）
        resp = requests.get(url, timeout=5, proxies={"http": None, "https": None})
    except Exception as e:  # noqa: BLE001
        return FAIL, f"{url} 请求失败：{type(e).__name__}", "确认容器/服务已启动"
    try:
        body = resp.json()
    except ValueError:
        return FAIL, f"{url} 返回的不是 JSON（HTTP {resp.status_code}）", "地址可能指向了别的服务"
    if resp.status_code == 200 and body.get("ok"):
        checks = body.get("checks", {})
        detail = "、".join(f"{k}={v.get('state')}" for k, v in checks.items())
        return PASS, f"{resp.status_code} ok｜{detail}", ""
    return FAIL, f"{resp.status_code} {body.get('status')}｜{json.dumps(body.get('checks', {}), ensure_ascii=False)}", (
        "看 checks 里哪一项 error：db=数据库 / data_dir=磁盘写不进去"
    )


def check_git_env_hygiene() -> tuple[str, str, str]:
    """`.env` 绝不能被追踪：这个项目 2026-10-23 就因为一份 .env.bak 泄露轮换过密钥"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=20
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return TODO, "读不到 git 文件列表（可能不是 git 仓库）", ""
    leaked = [
        f for f in out if Path(f).name.startswith(".env") and Path(f).name != ".env.example"
    ]
    if leaked:
        return FAIL, f"被追踪的密钥文件：{', '.join(leaked)}", "git rm --cached 并轮换密钥"
    return PASS, "仓库里只有 .env.example", ""


CHECKS = [
    ("密钥非示例值", check_secrets),
    ("数据目录可写", check_data_dir_writable),
    ("WAL 模式", check_wal_mode),
    ("备份存在", check_backup_dir),
    ("图片目录", check_images_dir),
    ("图片第二份", check_mirror_dir),
    ("调度器", check_scheduler),
    ("仓库无 .env", check_git_env_hygiene),
]

# 机器判不了、必须人看的（照抄 DEPLOY 第四节里剩下的那几条）
MANUAL = [
    "员工机器 PS 版本已确认（不同版本 COM 行为略有差异）",
    "已上传至少 1 个模板，并在客户端成功出过 1 张图（输出目录里只有「作者_标题.jpg」）",
    "生成完立刻关掉客户端再重开，后台仍能查到那条记录",
    "容器日志已限流（compose 的 logging 段），docker logs 不会无限长",
    "server/data 已纳入 NAS 定时备份（已演练过一次恢复）",
    "运维告警能收到：删掉一个备份文件后跑 python -m server.backup 验证",
    "后台「操作日志」页能看到本次登录记录",
]


def _row(name: str, status: str, detail: str, hint: str) -> dict:
    return {"item": name, "status": status, "detail": detail, "hint": hint}


# ---------- 终端对齐 ----------

def _display_width(text: str) -> int:
    """中文在等宽终端里占两格。用 len() 对齐的结果是整张表错位——
    而这张表是给人在上线前盯着看的，错位就等于没对齐。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def run(base_url: str = "") -> dict:
    rows = []
    for name, fn in CHECKS:
        try:
            status, detail, hint = fn()
        except Exception as e:  # noqa: BLE001 - 单项炸了不能拖垮整张表
            status, detail, hint = FAIL, f"检查本身出错：{type(e).__name__}: {e}", "把这个报错发给运维"
        rows.append(_row(name, status, detail, hint))

    status, detail, hint = check_readiness(base_url)
    rows.append(_row("就绪探针", status, detail, hint))

    return {
        "rows": rows,
        "manual": MANUAL,
        "failed": sum(1 for r in rows if r["status"] == FAIL),
        "passed": sum(1 for r in rows if r["status"] == PASS),
        "todo": sum(1 for r in rows if r["status"] == TODO),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="上线前自检（DEPLOY 第四节的可执行版）")
    parser.add_argument("--base-url", default="", help="服务地址，给了就顺带探 /health/ready")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给 CI）")
    args = parser.parse_args(argv)

    app_ctx = None
    try:  # 大部分检查要读 Config / 连库，需要一个 app context
        from server.app import create_app

        app = create_app()
        app_ctx = app.app_context()
        app_ctx.push()
    except Exception as e:  # noqa: BLE001
        print(f"无法初始化服务上下文：{type(e).__name__}: {e}", file=sys.stderr)
        print("（先确认 .env 已按 .env.example 配好）", file=sys.stderr)
        return 2

    try:
        report = run(args.base_url)
    finally:
        if app_ctx is not None:
            app_ctx.pop()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        label_w = _display_width("[不通过]")  # 最长的那种标记，三种状态都按它对齐
        width = max(_display_width(r["item"]) for r in report["rows"])
        detail_col = label_w + 2 + width + 2
        for r in report["rows"]:
            mark = {PASS: "通过", FAIL: "不通过", TODO: "需人工"}[r["status"]]
            print(f"{_pad(f'[{mark}]', label_w)}  {_pad(r['item'], width)}  {r['detail']}")
            if r["status"] == FAIL and r["hint"]:
                print(f"{' ' * detail_col}→ {r['hint']}")
        print()
        print(f"通过 {report['passed']} 项，不通过 {report['failed']} 项，需人工 {report['todo']} 项")
        print("机器判不了、必须人看的：")
        for item in report["manual"]:
            print(f"  [ ] {item}")

    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
