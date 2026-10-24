"""目录用量统计的缓存语义（server/app/dir_usage.py）

仪表盘底部那行「图片 12,345 张 · 8.2 GB」每 30 秒至少被读一次（仪表盘页面 +
`/api/v1/stats/overview`），旧实现每次 `rglob("*")` 全量遍历——几万张图就是
几万次 stat，打开仪表盘肉眼可见地卡。

这里钉的是缓存本身：**什么时候可以复用、什么时候必须重扫**。缓存写错的两种
典型后果都是"数字慢慢变得不对"，而没人会去核对磁盘占用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app import dir_usage as du


@pytest.fixture(autouse=True)
def fresh_cache():
    """缓存是模块级字典，用例之间必须隔离（否则顺序一换就互相污染）"""
    du.clear_cache()
    yield
    du.clear_cache()


class _Counter:
    """包一层扫描函数，数清"这次到底扫了几个目录" """

    def __init__(self):
        self.scanned: list[str] = []

    def __call__(self, path: Path):
        self.scanned.append(path.name)
        return du.scan_layer(path)

    def reset(self):
        self.scanned.clear()


def _write(path: Path, size: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _tree(root: Path) -> None:
    _write(root / "2026" / "10" / "24" / "a.jpg", 100)
    _write(root / "2026" / "10" / "24" / "b.jpg", 200)
    _write(root / "2026" / "10" / "23" / "c.jpg", 300)


def test_counts_every_file_and_byte(tmp_path):
    _tree(tmp_path)

    assert du.dir_usage(tmp_path) == (3, 600)


def test_missing_dir_is_zero_not_an_error(tmp_path):
    assert du.dir_usage(tmp_path / "没有这个目录") == (0, 0)


def test_second_call_does_not_rescan_unchanged_dirs(tmp_path):
    _tree(tmp_path)
    counter = _Counter()
    du.dir_usage(tmp_path, scan=counter)
    assert counter.scanned, "第一次总得真扫"

    counter.reset()
    assert du.dir_usage(tmp_path, scan=counter) == (3, 600)
    assert counter.scanned == [], f"目录没变还在重扫：{counter.scanned}"


def test_new_file_in_a_day_dir_is_noticed_immediately(tmp_path):
    """当天目录天天在变：新增一张图必须立刻算进去（缓存靠 mtime 失效）"""
    _tree(tmp_path)
    du.dir_usage(tmp_path)

    _write(tmp_path / "2026" / "10" / "24" / "d.jpg", 50)

    assert du.dir_usage(tmp_path) == (4, 650)


def test_change_deep_down_updates_the_parent_total(tmp_path):
    """这就是"只缓存子树合计"会错的地方：孙目录变了，上层的缓存合计会一直是旧值"""
    _tree(tmp_path)
    du.dir_usage(tmp_path)

    _write(tmp_path / "2026" / "10" / "23" / "e.jpg", 1000)

    assert du.dir_usage(tmp_path) == (4, 1600)


def test_expired_cache_is_rescanned(tmp_path):
    _tree(tmp_path)
    counter = _Counter()
    du.dir_usage(tmp_path, scan=counter, now=0.0, ttl=300)

    counter.reset()
    du.dir_usage(tmp_path, scan=counter, now=301.0, ttl=300)

    assert counter.scanned, "TTL 过了必须重扫（外部 rsync 改了文件但 mtime 没变的兜底）"


def test_unreadable_subdir_does_not_break_the_whole_row(tmp_path):
    _tree(tmp_path)

    def flaky(path: Path):
        if path.name == "23":
            raise OSError("权限不足")
        return du.scan_layer(path)

    # 23 目录整块算 0，其余照常——一行状态卡不能因为一个坏目录变红
    assert du.dir_usage(tmp_path, scan=flaky) == (2, 300)


def test_cache_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(du, "CACHE_MAX_DIRS", 2)
    _tree(tmp_path)  # 5 个目录：root/2026/10/24/23

    du.dir_usage(tmp_path)

    assert len(du._CACHE) <= 2, "缓存不设上限就是换个方式漏内存"
