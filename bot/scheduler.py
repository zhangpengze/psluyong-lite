"""定时任务：日报推送 / 次日终版巡检 / 夜间维护（备份 / 图片镜像 / 磁盘告警）

夜间维护为什么合成一个 job：
    备份、镜像、磁盘检查是同一件事的几个步骤，分别排期会出现"备份跑完了但
    镜像还没开始"的时间窗。合成一个 job 之后，任一步失败都能立刻带着上下文告警，
    日志里也只有一个时间段要查。

日报为什么是两个 job（18:00 推快报 + 次日 08:30 补终版）：
    见 bot/report_patrol.py 的文件头——18:00 之后的补录、以及"18:00 服务没起来"
    这两种情况都不是同一个 job 重跑能解决的。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .report_patrol import patrol

logger = logging.getLogger(__name__)

_scheduler = None


def nightly_job() -> None:
    """备份库 → 镜像图 → 检查磁盘。任一步失败都推群，不再只写日志。

    提到模块级（不依赖 app）：定时器里只是个引用，但测试要能直接调到它——
    嵌在 start_scheduler 里就只能起真调度器才能验证，那是本末倒置。
    """
    from server.app.config import BACKUP_DIR, IMAGES_DIR, Config
    from server.backup import (
        create_backup,
        db_file_path,
        dir_size_bytes,
        mirror_images,
    )
    from bot.report import push_ops_alert

    # 1) 数据库热备
    try:
        create_backup(db_file_path(), BACKUP_DIR, Config.BACKUP_KEEP)
    except Exception as e:  # noqa: BLE001
        logger.exception("定时备份执行失败")
        push_ops_alert("数据库备份失败", f"{type(e).__name__}: {e}")

    # 2) 成品图增量镜像（未配置则跳过，启动时已 warning 过一次）
    if Config.IMAGES_MIRROR_DIR:
        try:
            result = mirror_images(IMAGES_DIR, Path(Config.IMAGES_MIRROR_DIR))
            if result["failed"]:
                push_ops_alert(
                    "图片镜像有文件失败",
                    f"失败 {result['failed']} 个｜目标 {Config.IMAGES_MIRROR_DIR}",
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("图片镜像失败")
            push_ops_alert("图片镜像失败", f"{type(e).__name__}: {e}")

    # 3) 磁盘占用阈值
    try:
        used_gb = dir_size_bytes(IMAGES_DIR) / 1024 / 1024 / 1024
        if Config.DISK_ALERT_GB and used_gb >= Config.DISK_ALERT_GB:
            push_ops_alert(
                "成品图占用已超阈值",
                f"当前 {used_gb:.1f} GB ≥ 阈值 {Config.DISK_ALERT_GB:.0f}｜"
                "建议归档旧月份或扩容",
            )
    except Exception:  # noqa: BLE001
        logger.exception("磁盘占用检查失败")


def start_scheduler(app) -> None:
    """启动后台调度器。重复调用只生效一次。"""
    global _scheduler
    if _scheduler is not None:
        logger.info("调度器已在运行，跳过重复启动")
        return

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    from bot.report import push_ops_alert, run_daily
    from server.app.config import Config

    def job() -> None:
        try:
            result = run_daily(app, push=True)
            logger.info("定时日报完成: %s", result["message"])
            # 配了机器人却推送失败，必须让人知道——徽标在后台页里，没人会天天点开看
            if Config.NAPCAT_WEBHOOK and not result["pushed"]:
                push_ops_alert("定时日报未推送成功", f"日期 {result['date']}｜{result['message']}")
        except Exception as e:  # noqa: BLE001 - 定时任务不能因单次异常退出
            logger.exception("定时日报执行失败")
            push_ops_alert("定时日报生成异常", f"{type(e).__name__}: {e}"[:200])

    def patrol_job() -> None:
        """次日巡检：补昨天的终版（内容没变就不发），并报更早的缺口。

        缺口告警由 patrol 自己发（它知道是哪几天），这里只管"这次补推失败了"。
        """
        try:
            result = patrol(app)
            if Config.NAPCAT_WEBHOOK and not result["pushed"] and not result["skipped"]:
                push_ops_alert(
                    "日报终版补推失败", f"日期 {result['date']}｜{result['message']}"
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("日报巡检执行失败")
            push_ops_alert("日报巡检异常", f"{type(e).__name__}: {e}"[:200])

    _scheduler = BackgroundScheduler(timezone=Config.TZ)
    _scheduler.add_job(
        job,
        CronTrigger(hour=Config.DAILY_REPORT_HOUR, minute=Config.DAILY_REPORT_MINUTE),
        id="daily_report",
        replace_existing=True,
        # 只在"进程活着但被卡住"时有意义。进程重启后 job 根本不在 MemoryJobStore
        # 里，这个参数救不了——那种情况由 report_patrol 兜（次日发现并补推）。
        misfire_grace_time=3600,
        coalesce=True,
    )
    _scheduler.add_job(
        patrol_job,
        CronTrigger(hour=Config.REPORT_FINAL_HOUR, minute=Config.REPORT_FINAL_MINUTE),
        id="report_patrol",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    _scheduler.add_job(
        nightly_job,
        CronTrigger(hour=Config.BACKUP_HOUR, minute=Config.BACKUP_MINUTE),
        id="nightly_maintenance",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        "调度器已启动：日报每天 %02d:%02d、终版巡检每天 %02d:%02d、夜间维护每天 %02d:%02d（%s）",
        Config.DAILY_REPORT_HOUR,
        Config.DAILY_REPORT_MINUTE,
        Config.REPORT_FINAL_HOUR,
        Config.REPORT_FINAL_MINUTE,
        Config.BACKUP_HOUR,
        Config.BACKUP_MINUTE,
        Config.TZ,
    )
    if not Config.IMAGES_MIRROR_DIR:
        logger.warning(
            "未配置 IMAGES_MIRROR_DIR：成品图没有第二份副本，盘坏即全部丢失。"
            "建议指向另一块盘或另一台 NAS 的共享目录。"
        )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
