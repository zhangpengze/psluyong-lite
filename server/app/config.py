"""
服务端配置

所有可变项走环境变量，密钥类没有弱默认值——缺了就启动失败，
避免"能跑起来但用着弱口令"这种更危险的状态。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # server/
# 统一在仓库根目录放 .env，不论从哪个目录启动都能读到
load_dotenv(BASE_DIR.parent / ".env")

if sys.platform == "win32":
    # Windows 上 Python 会采信 TZ 环境变量，但 tz 数据库缺失时 tzset() 会退化成 UTC，
    # 结果日志时间与 astimezone() 全部偏 8 小时。展示时区已由 Config.TZ + zoneinfo
    # 显式负责，这里让 TZ 不参与，日志才能显示本机真实时间。
    os.environ.pop("TZ", None)

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
IMAGES_DIR = Path(os.getenv("IMAGES_DIR", DATA_DIR / "images"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", DATA_DIR / "backups"))


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"环境变量 {name} 未设置。请复制 .env.example 为 .env 并填写后再启动。"
        )
    return value


class Config:
    # Flask
    SECRET_KEY = os.getenv("SECRET_KEY") or _required("SECRET_KEY")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("FORCE_HTTPS", "0") == "1"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12  # 12 小时
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "64")) * 1024 * 1024

    # 数据库
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # 认证
    API_KEY = os.getenv("API_KEY") or _required("API_KEY")
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or _required("ADMIN_PASSWORD")

    # 日报
    DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "18"))
    DAILY_REPORT_MINUTE = int(os.getenv("DAILY_REPORT_MINUTE", "0"))
    NAPCAT_WEBHOOK = os.getenv("NAPCAT_WEBHOOK", "").strip()
    NAPCAT_TOKEN = os.getenv("NAPCAT_TOKEN", "").strip()
    REPORT_GROUP_ID = os.getenv("REPORT_GROUP_ID", "").strip()

    # QQ 机器人反向指令（群里发「汇总」→ 私聊回复当天日报）
    # 白名单：允许触发指令的 QQ 号，逗号分隔。**留空 = 谁都不能用**（fail-closed）。
    # 2026-10-24 改：原来是"留空=不限制"，与"内网信任边界"叠加后等于——机器人被
    # 拉进任何群，群里任何人发一句「汇总」就能把含员工姓名/作者/标题/失败原因的
    # 报表私聊带走。要放宽就显式列出 QQ 号，别靠"留空"表达。
    BOT_ADMIN_QQ_IDS = os.getenv("BOT_ADMIN_QQ_IDS", "").strip()
    # 允许触发指令的群号，逗号分隔。留空 = 只允许 REPORT_GROUP_ID 这一个群。
    # 单独一个键的原因：机器人常同时待在日报群和一个管理群里。
    BOT_ALLOWED_GROUP_IDS = os.getenv("BOT_ALLOWED_GROUP_IDS", "").strip()
    # 回调校验密钥：NapCat 上报请求头 X-Bot-Secret 带同一个值才处理。
    # **留空 = 拒绝所有回调**（fail-closed），旧行为是留空即放行。
    BOT_CALLBACK_SECRET = os.getenv("BOT_CALLBACK_SECRET", "").strip()
    # 逃生阀：确实在完全隔离的内网/本机调试时，显式置 1 才允许"无密钥回调"。
    BOT_ALLOW_INSECURE = os.getenv("BOT_ALLOW_INSECURE", "0") == "1"
    # 同一人触发指令的冷却秒数，防连点/重复上报
    BOT_COOLDOWN_S = int(os.getenv("BOT_COOLDOWN_S", "10"))

    # 失败即时告警：录用失败立刻推群，不等当天日报
    ALERT_ON_FAILURE = os.getenv("ALERT_ON_FAILURE", "1") == "1"
    # 告警最小间隔秒数：PS 故障时批量失败不会把群刷爆
    ALERT_MIN_INTERVAL_S = int(os.getenv("ALERT_MIN_INTERVAL_S", "5"))

    # 其他
    TZ = os.getenv("TZ", "Asia/Shanghai")
    ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "1") == "1"

    # 数据库备份：每天自动一份，保留最近 N 份；手动随时 python -m server.backup
    BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "10"))
    BACKUP_HOUR = int(os.getenv("BACKUP_HOUR", "3"))
    BACKUP_MINUTE = int(os.getenv("BACKUP_MINUTE", "30"))

    # 成品图镜像目录：留空 = 不镜像。指向另一块盘 / 另一台 NAS 的共享目录。
    # 数据库有备份而图片没有，等于只保住了索引——这才是最容易丢的东西。
    IMAGES_MIRROR_DIR = os.getenv("IMAGES_MIRROR_DIR", "").strip()

    # 图片占用告警阈值（GB）：超过就推一次群，一天最多一次，避免刷屏
    DISK_ALERT_GB = float(os.getenv("DISK_ALERT_GB", "20"))

    # 外部告警开关：备份失败 / 图片镜像失败 / 磁盘超阈值 / 定时日报推送失败时推群。
    # 复用失败的推送通道（NAPCAT_WEBHOOK），没配就只记日志。
    ALERT_ON_OPS_FAILURE = os.getenv("ALERT_ON_OPS_FAILURE", "1") == "1"


def ensure_dirs() -> None:
    for path in (DATA_DIR, IMAGES_DIR, BACKUP_DIR):
        path.mkdir(parents=True, exist_ok=True)
