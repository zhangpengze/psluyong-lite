"""补传用的成品图缓冲

离线队列原来只存图片的**路径**（pending/*.json 里一个字符串）。员工把输出目录
整理一下、或者把当天那批图挪去别处，队列里那条记录就永久失去了图：补传时
api_client 发现文件不在，会**不带文件**把记录提交上去——服务端落库一条没有图
的记录，界面还显示"已同步"。数据看着完整，实际废了，而且没人会知道。

现在入队时就把图复制进本目录（`{item_id}.jpg`，与队列里的 `{item_id}.json` 同名），
补传优先用这份副本：
  - 员工清空输出目录 → 副本还在，照样补传成功
  - 副本也没了（有人清了 %APPDATA%）→ **不提交**，整条记录转死信留档，
    界面显示"死信 N 条"，员工可以点"重试死信"重新来一遍
复制失败（磁盘满、杀软锁文件）只记一条日志：记录照常入队，退回用原路径，
绝不能让"复制不了图"把整条记录也丢了。

代价是每台机器多占一份磁盘，所以有保留期：超过 RETENTION_DAYS 天、且队列和
死信里都已经没有对应 json 的副本会被清掉（进程补传中途被杀、json 没写成之类
留下的孤儿）。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from .config_schema import IMAGE_BUFFER_DIR

logger = logging.getLogger(__name__)

BUFFER_DIR = IMAGE_BUFFER_DIR
# 积压一般个位数，副本留 7 天足够，又不至于让一台机器常年多占几个 GB
RETENTION_DAYS = 7
_DAY = 24 * 3600


def _ensure() -> None:
    BUFFER_DIR.mkdir(parents=True, exist_ok=True)


def store(source: Path, item_id: str) -> Path | None:
    """把成品图复制进缓冲。失败返回 None——调用方据此退回用原路径。"""
    try:
        _ensure()
        target = BUFFER_DIR / f"{item_id}{source.suffix or '.jpg'}"
        shutil.copy2(source, target)
        return target
    except OSError as e:
        logger.warning("成品图复制到补传缓冲失败（记录照常入队，改用原路径）: %s", e)
        return None


def locate(item: dict) -> tuple[Path | None, bool]:
    """这一条记录该用哪张图。返回 (路径, 是否已丢失)。

    第二项为 True 只在"记录声明过有图、但缓冲和原目录都找不到"时出现。
    这时调用方必须把记录转死信，**不能**退回成"不带图提交"——那正是要修的 bug。
    """
    declared = False
    for key in ("buffered_image", "image_path"):
        raw = str(item.get(key) or "").strip()
        if not raw:
            continue
        declared = True
        path = Path(raw)
        if path.is_file():
            return path, False
    return None, declared


def discard(item: dict) -> None:
    """记录销毁（补传成功 / 业务拒绝）后删副本，否则会在 %APPDATA% 里越积越多。"""
    raw = str(item.get("buffered_image") or "").strip()
    if raw:
        Path(raw).unlink(missing_ok=True)


def to_dead(item: dict, target_dir: Path, stem: str) -> None:
    """死信留档要自带图：把副本挪进 dead/，人工重试时还能补上。"""
    raw = str(item.get("buffered_image") or "").strip()
    if not raw:
        return
    src = Path(raw)
    if not src.is_file():
        return
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / f"{stem}{src.suffix or '.jpg'}"
        os.replace(src, dest)
        item["buffered_image"] = str(dest)
    except OSError as e:
        # 图留在缓冲里也不丢数据，最坏情况是按保留期被清掉
        logger.warning("死信留档时挪图失败（记录已留档，图仍在缓冲）: %s", e)


def restore(item: dict) -> None:
    """死信回到队列时把图从 dead/ 搬回缓冲，别让图长期压在死信目录里。"""
    raw = str(item.get("buffered_image") or "").strip()
    if not raw:
        return
    src = Path(raw)
    if not src.is_file() or src.parent == BUFFER_DIR:
        return
    try:
        _ensure()
        dest = BUFFER_DIR / src.name
        os.replace(src, dest)
        item["buffered_image"] = str(dest)
    except OSError as e:
        logger.warning("死信回队列时挪图失败（继续用原路径的副本）: %s", e)


def prune(pending_dir: Path, dead_dir: Path, days: int = RETENTION_DAYS, now: float | None = None) -> int:
    """清孤儿副本：队列和死信里都没有同名 json、且已过保留期。

    只删"过期且没人引用"的：正在重试的记录一定还在 pending/ 或 dead/ 有 json，
    所以这个规则永远不会删掉活记录的图。
    """
    if not BUFFER_DIR.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - days * _DAY
    removed = 0
    for path in BUFFER_DIR.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        if (pending_dir / f"{path.stem}.json").exists():
            continue
        if (dead_dir / f"{path.stem}.json").exists():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            logger.warning("清理过期补传图失败 %s: %s", path.name, e)
    return removed
