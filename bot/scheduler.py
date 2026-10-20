"""定时任务：每天固定时间推送日报 + 自动备份数据库"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_scheduler = None


def start_scheduler(app) -> None:
    """启动后台调度器。重复调用只生效一次。"""
    global _scheduler
    if _scheduler is not None:
        logger.info("调度器已在运行，跳过重复启动")
        return

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    from bot.report import run_daily
    from server.app.config import Config

    def job() -> None:
        try:
            result = run_daily(app, push=True)
            logger.info("定时日报完成: %s", result["message"])
        except Exception:  # noqa: BLE001 - 定时任务不能因单次异常退出
            logger.exception("定时日报执行失败")

    def backup_job() -> None:
        from server.app.config import BACKUP_DIR
        from server.backup import create_backup, db_file_path

        try:
            create_backup(db_file_path(), BACKUP_DIR, Config.BACKUP_KEEP)
        except Exception:  # noqa: BLE001
            logger.exception("定时备份执行失败")

    _scheduler = BackgroundScheduler(timezone=Config.TZ)
    _scheduler.add_job(
        job,
        CronTrigger(hour=Config.DAILY_REPORT_HOUR, minute=Config.DAILY_REPORT_MINUTE),
        id="daily_report",
        replace_existing=True,
        misfire_grace_time=3600,  # 服务重启错过时间点，一小时内仍补跑
        coalesce=True,
    )
    _scheduler.add_job(
        backup_job,
        CronTrigger(hour=Config.BACKUP_HOUR, minute=Config.BACKUP_MINUTE),
        id="db_backup",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        "调度器已启动：日报每天 %02d:%02d，备份每天 %02d:%02d（%s）",
        Config.DAILY_REPORT_HOUR,
        Config.DAILY_REPORT_MINUTE,
        Config.BACKUP_HOUR,
        Config.BACKUP_MINUTE,
        Config.TZ,
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
