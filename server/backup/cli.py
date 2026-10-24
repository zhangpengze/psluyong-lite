"""备份 CLI：`python -m server.backup [--list|--restore|--mirror-images]`。

恢复是全流程里最容易把库搞坏的操作，所以恢复前强制：
    1) 解压后 PRAGMA integrity_check
    2) 校验核心表都在
    3) 当前库先备份为 .pre-restore-*.db（失败可手工退回）
（校验与留档实现在 store.restore_backup，这里只解析参数、分发、报错码。）
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from .mirror import mirror_images
from .store import create_backup, db_file_path, list_backups, restore_backup

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    from server.app.config import BACKUP_DIR, IMAGES_DIR, Config

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="数据库备份 / 恢复 / 图片镜像")
    parser.add_argument("--keep", type=int, default=Config.BACKUP_KEEP, help="保留最近几份")
    parser.add_argument("--list", action="store_true", help="列出现有备份")
    parser.add_argument("--restore", metavar="FILE", help="恢复指定备份")
    parser.add_argument("--rehearse", action="store_true", help="只校验不落盘")
    parser.add_argument(
        "--mirror-images",
        nargs="?",
        const="",
        metavar="TARGET",
        help="把成品图增量镜像到 TARGET（不填则用 .env 的 IMAGES_MIRROR_DIR）",
    )
    args = parser.parse_args(argv)

    try:
        if args.list:
            for path in list_backups(BACKUP_DIR):
                size_kb = path.stat().st_size / 1024
                print(f"{path.name}  {size_kb:.1f} KB")
            if not list_backups(BACKUP_DIR):
                print("（暂无备份）")
            return 0

        if args.mirror_images is not None:
            target = args.mirror_images.strip() or Config.IMAGES_MIRROR_DIR
            if not target:
                logger.error("未配置镜像目录：传 --mirror-images <目录> 或在 .env 设 IMAGES_MIRROR_DIR")
                return 1
            result = mirror_images(IMAGES_DIR, Path(target))
            # 有单张失败就返回非 0，让定时任务的告警能触发
            return 1 if result["failed"] else 0

        if args.restore:
            restore_backup(Path(args.restore), db_file_path(), rehearse=args.rehearse)
            return 0

        create_backup(db_file_path(), BACKUP_DIR, args.keep)
        return 0
    except (RuntimeError, FileNotFoundError, sqlite3.Error) as e:
        logger.error("%s", e)
        return 1
