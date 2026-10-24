"""数据库自动备份与恢复（入口门面）。

用法：
    python -m server.backup                 # 立即备份一次
    python -m server.backup --list          # 列出现有备份
    python -m server.backup --restore <文件>            # 恢复（自动先备份当前库）
    python -m server.backup --restore <文件> --rehearse # 彩排：只校验不动真库

按子功能拆分（单文件 ≤300 行门禁）：
    store.py   SQLite 在线热备 / 完整性校验 / 保留 N 份 / 恢复留档
    mirror.py  成品图增量镜像与目录体积统计
    cli.py     argparse 参数解析与错误码（`python -m server.backup` 经 __main__.py 调它）
本文件 re-export 公开函数，`from server.backup import create_backup` 与
`import server.backup as backup_mod` 的 monkeypatch 用法都保持原样可用。
"""

from __future__ import annotations

from .cli import main
from .mirror import dir_size_bytes, mirror_images
from .store import (
    REQUIRED_TABLES,
    create_backup,
    db_file_path,
    list_backups,
    restore_backup,
)

__all__ = [
    "REQUIRED_TABLES",
    "create_backup",
    "db_file_path",
    "dir_size_bytes",
    "list_backups",
    "main",
    "mirror_images",
    "restore_backup",
]
