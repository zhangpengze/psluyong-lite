"""单进程检测：这套服务只能开一份，开两份日报会各推一遍。

为什么这是硬约束而不是"顺手加个锁"：
    日报的"今天推过没有"是「读标记 → 发消息 → 写标记」三步（bot/report_store.py），
    三步之间没有锁。两个进程同时走到 18:00，都会读到 pushed=false，于是各发一遍
    一模一样的日报；夜间备份同理，会往同一个 backups/ 写两批 .gz 再互相挤掉。
    正统解法是抢锁选主（谁拿到锁谁干活），但那要跨平台文件锁 + 心跳 +
    主挂了接管，在一台 NAS、一个容器的部署里没有对应收益，只多一层会坏的零件。
    所以：约束写进文档（docs/DEPLOY.md「必须单进程运行」），代码只负责**发现**。

本模块只报警，不阻止任何事：
    拿不到锁照样启动、照常推日报——服务起不来比多推一条群消息严重得多。
    它唯一回答的问题是"现在是不是有第二份在跑"，答案进启动日志和仪表盘服务行。

为什么用文件锁，而不是写 pid 文件再回头看它活没活：
    Docker 里每个容器有独立的 PID 命名空间，容器内的服务基本都是 1 号进程——
    跨容器"看看这个 pid 还在吗"既看不到别人的进程表，也不可信（pid 会被重用，
    两个容器很可能都写着 pid=1）。而 flock / msvcrt.locking 由内核记账：
    进程被 SIGKILL 杀掉锁也立刻释放，不需要超时清理，也不会把崩溃残留的
    空文件误当成"另一个副本还活着"。

它什么时候会看错（都不影响功能）：
    - 两份副本不共享 DATA_DIR：根本碰不上，也检测不到——但那种部署本来
      就不会重复推送同一份数据。
    - DATA_DIR 落在 NFS / SMB 上：网络文件系统的锁语义不保证。可能两边都拿到
      （→ 探不出来，退化成"只有文档"），也可能两边都拿不到（→ 每次启动
      警告一句，日志里会写明这是启发式判断）。
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 锁文件位置：DATA_DIR/run/instance.lock
# 放 run/ 而不是 DATA_DIR 根下——根下已经有 images/ backups/ reports/ 这些
# 业务目录，运行时杂项堆在一起，清理备份的人容易顺手删错。
RUN_SUBDIR = "run"
LOCK_NAME = "instance.lock"

# 锁只覆盖第 0 字节：够用来表达"有人在"；写进去的内容只是给人看的备注。
_LOCK_BYTES = 1

_fd: int | None = None
_state: dict[str, Any] = {"checked": False, "single": True, "detail": ""}


def lock_path(data_dir: str | Path | None = None) -> Path:
    """锁文件路径。不传 data_dir 时用当前配置的 DATA_DIR。"""
    if data_dir is None:
        from server.app.config import DATA_DIR

        data_dir = DATA_DIR
    return Path(data_dir) / RUN_SUBDIR / LOCK_NAME


def _try_lock(fd: int) -> bool:
    """非阻塞试锁。拿不到返回 False，绝不等待（等待会把启动卡成假死）。"""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            # LK_NBLCK 不重试（LK_LOCK 才会重试十次，那会让启动慢 10 秒）
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_BYTES)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _stamp(fd: int) -> None:
    """把持有者写进锁文件：SSH 上去 cat 一眼就知道是谁占着，不用猜。

    只在 Linux（真正的部署目标 NAS）上读得回来：Windows 锁住的字节连别的句柄
    读都禁止，所以那边的备注只有持有者自己看得见。读不到就是"占用者未知"，
    判定本身不依赖它。
    """
    line = f"pid={os.getpid()} host={socket.gethostname()} at={datetime.now():%Y-%m-%d %H:%M:%S}\n"
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, line.encode("utf-8"))
    except OSError as e:  # 写不进只是少了条线索，不影响判定
        logger.debug("写单进程锁文件失败: %s", e)


def _holder(path: Path) -> str:
    """读锁文件里的持有者备注，读不到（含被锁住不许读）就返回空串。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text.splitlines()[0][:120] if text else ""


def _remember(single: bool, detail: str) -> None:
    _state.update(checked=True, single=single, detail=detail)


def acquire(data_dir: str | Path | None = None) -> tuple[bool, str]:
    """拿一次锁，把结论记住。返回 (是否唯一, 一行说明)。

    重复调用直接返回首次结论：仪表盘、探针、启动流程都会来问"是不是多副本"，
    每次都重开文件没有意义，也会把 fd 越攒越多。
    """
    global _fd
    if _state["checked"]:
        return _state["single"], _state["detail"]

    path = lock_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        # 连文件都建不了（只读卷 / 权限不对）：判断不了，就当没问题。
        # 这里唯一允许犯的错是"漏报一次"，绝不能变成"每次都误报多副本"。
        logger.debug("单进程检测跳过：无法创建锁文件 %s（%s）", path, e)
        _remember(True, "")
        return True, ""

    if _try_lock(fd):
        _fd = fd  # 故意不关：锁跟着 fd 活着，进程退出时内核自动释放
        _stamp(fd)
        _remember(True, "")
        return True, ""

    detail = f"检测到另一份服务在运行（{_holder(path) or '占用者未知'}）"
    try:
        os.close(fd)
    except OSError:
        pass
    _remember(False, detail)
    return False, detail


def warn_if_duplicate(data_dir: str | Path | None = None) -> bool:
    """启动时调一次：多副本就大声说一句。

    刻意不抛、不返回致命值、不改控制流——这个函数的全部产物是一行日志。
    """
    single, detail = acquire(data_dir)
    if not single:
        logger.warning(
            "%s。本服务必须单进程运行（见 docs/DEPLOY.md「必须单进程运行」）："
            "日报的「今天推过没有」是读—发—写三步，两个进程会各推一遍。"
            "若确认只跑了一份，说明文件锁在网络盘上不生效，可忽略本条。",
            detail,
        )
    return single


def describe() -> tuple[bool, str]:
    """给仪表盘用：(是否唯一, 一行说明)。

    没检测过时返回 (True, "")——不知道的事不要拿来吓人。
    """
    return _state["single"], _state["detail"]


def reset() -> None:
    """测试用：放掉锁并回到"没检查过"。生产代码不需要调。

    注意必须把 checked 也清掉：只清 single/detail 的话，下一个 acquire 会
    直接返回缓存的"没问题"，根本没去碰文件系统——用例于是绿得毫无意义。
    """
    global _fd
    if _fd is not None:
        try:
            os.close(_fd)
        except OSError:
            pass
        _fd = None
    _state.update(checked=False, single=True, detail="")
