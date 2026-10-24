"""目录用量统计（按目录缓存，避免每次全量遍历）

仪表盘底部「图片存储」那行要报"多少张、占多少 GB"，而它每 30 秒被至少读一次
（仪表盘页面 + `/api/v1/stats/overview`）。旧实现每次 `rglob("*")`：
几万张图就是几万次 stat，而且全是重复劳动——图片按 年/月/日 分层、文件名是
uuid，写进去就不再变。

缓存键是**目录自己这一层**（文件数 / 字节数 / 子目录列表），不是整棵子树的合计：

  - 目录里新增、删除、改名都会改该目录的 mtime，所以"mtime 没变"就等于
    "这一层的内容没变"。
  - 合计每次用缓存的各层结果**重新加**，所以孙目录里删了一个文件，
    上层数字立刻是对的（只缓存合计的话会一直报旧值，直到 TTL 过期）。
  - 一次遍历只剩"每个目录一次 stat"，一年也就几百个日目录。

TTL 是兜底：外部 rsync / 备份恢复改了文件但 mtime 恰好没变时，最多错 5 分钟。

扫描函数可注入（`scan=`），测试才能数清"这次到底扫了几个目录"——否则缓存
有没有生效只能靠猜。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_TTL_S = 300.0
# 每个目录一条记录；一年 365 个日目录，4096 足够，超了整体清空重建
CACHE_MAX_DIRS = 4096

# path -> (mtime_ns, 本层文件数, 本层字节数, 子目录, 记下来的时刻)
_CACHE: dict[str, tuple[int, int, int, tuple[Path, ...], float]] = {}
_LOCK = threading.Lock()


def scan_layer(path: Path) -> tuple[int, int, tuple[Path, ...]]:
    """只列一层：返回 (本层文件数, 本层字节数, 子目录列表)

    不跟随软链接：备份目录里指向别处的链接会把无关的整盘算进来，
    而"我到底占了多少"这个问题只认本目录容得下的实体。
    """
    files = total = 0
    subdirs: list[Path] = []
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files += 1
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                # 权限/坏链接：跳过这个条目，不让一行状态卡整块失败
                continue
    return files, total, tuple(subdirs)


def _walk(path: Path, scan, ttl: float, now: float) -> tuple[int, int]:
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return 0, 0

    key = str(path)
    with _LOCK:
        cached = _CACHE.get(key)
    if cached and cached[0] == stamp and now - cached[4] < ttl:
        layer_files, layer_bytes, subdirs = cached[1], cached[2], cached[3]
    else:
        try:
            layer_files, layer_bytes, subdirs = scan(path)
        except OSError as e:
            logger.warning("目录统计失败 %s: %s", path, e)
            return 0, 0
        with _LOCK:
            if len(_CACHE) >= CACHE_MAX_DIRS:
                _CACHE.clear()
            _CACHE[key] = (stamp, layer_files, layer_bytes, subdirs, now)

    files, total = layer_files, layer_bytes
    for sub in subdirs:
        sub_files, sub_bytes = _walk(sub, scan, ttl, now)
        files += sub_files
        total += sub_bytes
    return files, total


def dir_usage(root, scan=None, ttl: float = CACHE_TTL_S, now: float | None = None):
    """统计 root 下的文件数与总字节。目录不存在返回 (0, 0)。"""
    root = Path(root)
    if not root.exists():
        return 0, 0
    return _walk(root, scan or scan_layer, ttl, time.monotonic() if now is None else now)


def clear_cache() -> None:
    """测试与"刚恢复完备份"时用：立刻丢掉所有缓存"""
    with _LOCK:
        _CACHE.clear()
