"""成品图增量镜像与目录体积统计（图片侧备份）。

为什么需要：数据库每天有热备，图片却只在 IMAGES_DIR 这一块盘上。
盘坏、误删目录、误格式化，图就全没了——而图才是员工一天的工作成果，
库里剩的只是一串路径。备份数据库不备份图片，等于只保住了索引。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def mirror_images(images_dir: Path, mirror_dir: Path) -> dict:
    """把成品图增量镜像到第二个目录（另一块盘 / 另一台 NAS 共享）。

    为什么用"增量镜像"而不是每天打 tar：
      - 打 tar 每天一份，几十 GB 的图备份一次要跑十几分钟，还会迅速吃满备份盘；
      - 镜像只补新增/变化的文件，10 个员工一天几十张图，秒级完成；
      - 恢复时直接对着目录拷回去，不需要解包。

    判定"需要复制"：目标不存在，或大小不同。不比对 mtime——拷贝工具会改 mtime，
    比 mtime 会导致每天全量重拷一遍（WAL 那套经验的反面）。
    """
    if not images_dir.exists():
        raise FileNotFoundError(f"图片目录不存在: {images_dir}")

    mirror_dir.mkdir(parents=True, exist_ok=True)
    copied = skipped = failed = 0
    copied_bytes = 0

    for root, _dirs, files in os.walk(images_dir):
        rel = Path(root).relative_to(images_dir)
        target_dir = mirror_dir / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            src = Path(root) / name
            dst = target_dir / name
            try:
                if dst.exists() and dst.stat().st_size == src.stat().st_size:
                    skipped += 1
                    continue
                # 先写临时文件再替换：中途断电不会留下半张"看起来存在"的图
                tmp = dst.with_suffix(dst.suffix + ".tmp")
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
                copied += 1
                copied_bytes += dst.stat().st_size
            except OSError as e:
                # 单张失败不该中断整轮镜像，但要计数让上层告警
                failed += 1
                logger.warning("图片镜像失败 %s: %s", src, e)

    logger.info(
        "图片镜像完成：新增/更新 %s，跳过 %s，失败 %s，共 %.1f MB -> %s",
        copied,
        skipped,
        failed,
        copied_bytes / 1024 / 1024,
        mirror_dir,
    )
    return {"copied": copied, "skipped": skipped, "failed": failed, "bytes": copied_bytes}


def dir_size_bytes(root: Path) -> int:
    """目录总大小（图片占用告警用）。走不通就当 0，不影响主流程。"""
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total
