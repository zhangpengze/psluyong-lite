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
    # 次日终版巡检时点。18:00 推出去的是「当日快报」，18:00 之后员工加班补录的
    # 记录（work_date 仍是当天）不在里面；第二天这个点重算一遍才是终版——
    # 内容变了才补推「（终版）」，没变一条消息都不发。
    # 同时兜住"18:00 服务没起来、当天完全没推"：这是 MemoryJobStore 补不回来的。
    REPORT_FINAL_HOUR = int(os.getenv("REPORT_FINAL_HOUR", "8"))
    REPORT_FINAL_MINUTE = int(os.getenv("REPORT_FINAL_MINUTE", "30"))
    # 巡检时往前看几天找"没推成功的日报"，只汇总成一条运维告警，
    # 不自动往群里补推（否则一次宕机 5 天，群里会被 5 份旧日报淹没）
    REPORT_MISS_LOOKBACK_DAYS = int(os.getenv("REPORT_MISS_LOOKBACK_DAYS", "7"))

    # 官方 QQ 机器人（QQ 开放平台 /v2 OpenAPI）。三个值都在 q.qq.com 后台拿。
    # 为什么不走"登录一个真 QQ 号"的第三方协议端（NapCat 等）：那是把真号当机器人，
    # 且 NapCat 的授权明确禁止未经许可的衍生分发——打进安装包就是在分发。
    QQ_BOT_APPID = os.getenv("QQ_BOT_APPID", "").strip()
    QQ_BOT_SECRET = os.getenv("QQ_BOT_SECRET", "").strip()
    # 群的 openid，**不是群号**：它是机器人进群/@ 事件里带的 group_openid，
    # 填成 123456789 这种群号只会被平台判 40054002。
    QQ_BOT_GROUP_OPENID = os.getenv("QQ_BOT_GROUP_OPENID", "").strip()
    # 接口域名。2026-08-10 起官方统一到 api.bot.qq.com（旧 api.sgroup.qq.com 已下线），
    # 取 access_token 也在同一个域名下；留成可配是为了内网反代/私有网关。
    QQ_BOT_API_BASE = os.getenv("QQ_BOT_API_BASE", "").strip()

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
    # 复用日报的推送通道（QQ_BOT_*），没配就只记日志 + 落本地告警流水。
    ALERT_ON_OPS_FAILURE = os.getenv("ALERT_ON_OPS_FAILURE", "1") == "1"


def ensure_dirs() -> None:
    for path in (DATA_DIR, IMAGES_DIR, BACKUP_DIR):
        path.mkdir(parents=True, exist_ok=True)
