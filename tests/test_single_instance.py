"""单进程检测：多副本要能被发现，且**绝不能**因为发现而阻止启动。

这里的分寸是整件事的关键：本服务的"今天推过没有"是读—发—写三步无锁，
两个进程会各推一遍日报，所以必须单进程（约束写在 docs/DEPLOY.md）。
但代码只做检测——拿不到锁照样起、照常推，多推一条群消息远好过服务起不来。
下面 test_多副本不许拦住调度器启动 就是钉这条的。
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bot import single_instance
from bot import scheduler as scheduler_mod


@pytest.fixture(autouse=True)
def _fresh_state():
    """每个用例从"没检查过"开始，并且用完整条 fd——否则上一条的锁会留到这一条。"""
    single_instance.reset()
    yield
    single_instance.reset()


@contextlib.contextmanager
def _foreign_holder(path: Path, note: str = ""):
    """模拟"另一份服务正占着锁"。

    同进程里另开一个 fd 也是**独立的 open file description**，flock / msvcrt
    一样会冲突，所以不必真起第二个进程就能复现现场。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    assert single_instance._try_lock(fd), "占位锁没抢到，用例前提不成立"
    if note:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, note.encode("utf-8"))
    try:
        yield fd
    finally:
        os.close(fd)


# ---------- 锁文件在哪 ----------

def test_锁文件落在_data_dir_的_run_子目录(tmp_path):
    """位置是文档承诺的契约：部署文档让你去 cat 它，换位置就得改文档。"""
    assert single_instance.lock_path(tmp_path) == tmp_path / "run" / "instance.lock"


def test_默认用配置里的_data_dir(tmp_path, monkeypatch):
    from server.app import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    assert single_instance.lock_path() == tmp_path / "run" / "instance.lock"


# ---------- 单副本 ----------

def _read_through_fd(fd: int) -> str:
    """从自己手里的 fd 读锁文件内容。

    不能另开句柄读：Windows 上被锁住的那段字节连读都禁止（部署目标 NAS 是
    Linux，那边 `cat` 直接能看）。这里验证的是"内容有没有写进去"。
    """
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, 4096).decode("utf-8", "replace")


def test_单副本拿到锁并写下持有者(tmp_path):
    single, detail = single_instance.acquire(tmp_path)

    assert single is True and detail == ""
    assert single_instance._fd is not None
    text = _read_through_fd(single_instance._fd)
    assert "pid=%d" % os.getpid() in text, (
        "锁文件里要写清是谁占着，SSH 上去 cat 一眼就能定位，不用猜"
    )


def test_重复调用只判一次(tmp_path, monkeypatch):
    """仪表盘 + 探针 + 启动流程都会来问，不能每次都重开文件把 fd 攒起来。"""
    calls: list[int] = []
    real = single_instance._try_lock

    def counting(fd):
        calls.append(fd)
        return real(fd)

    monkeypatch.setattr(single_instance, "_try_lock", counting)
    single_instance.acquire(tmp_path)
    single_instance.acquire(tmp_path)
    single_instance.acquire(tmp_path)

    assert len(calls) == 1
    assert len(list((tmp_path / "run").iterdir())) == 1, "只该有一个锁文件，不能一次调用留一个"


def test_单副本不打多副本警告(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="bot.single_instance"):
        single_instance.warn_if_duplicate(tmp_path)

    assert not [r for r in caplog.records if "另一份服务" in r.getMessage()]


def test_没检测过不冒充多副本():
    """启动流程没跑到（比如 ENABLE_SCHEDULER=0）时，仪表盘不许编一条警告出来。"""
    assert single_instance.describe() == (True, "")


# ---------- 多副本 ----------

def test_撞上别人的锁要报出来(tmp_path):
    path = single_instance.lock_path(tmp_path)
    with _foreign_holder(path, note="pid=99 host=nas at=2026-10-25 18:00:00\n"):
        single, detail = single_instance.acquire(tmp_path)

    assert single is False
    assert "另一份服务在运行" in detail
    if os.name != "nt":
        # Windows 上被锁的字节读不出来（部署目标 NAS 是 Linux，读得到），
        # 所以"带上占用者"这条只在能读的平台断言——判定本身与它无关。
        assert "pid=99" in detail, "要带上占用者是谁，否则运维只能猜"


def test_多副本要写进警告日志(tmp_path, caplog):
    with _foreign_holder(single_instance.lock_path(tmp_path)):
        with caplog.at_level(logging.WARNING, logger="bot.single_instance"):
            single_instance.warn_if_duplicate(tmp_path)

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "必须单进程运行" in text, "警告要说明后果和该看哪份文档"
    assert "docs/DEPLOY.md" in text
    assert "各推一遍" in text, "要讲清到底坏在哪，否则会被当成噪音"


def test_占用者未知也要给出结论(tmp_path):
    """锁文件被清空 / 读不出来时，判定不能跟着消失。"""
    path = single_instance.lock_path(tmp_path)
    with _foreign_holder(path):
        single, detail = single_instance.acquire(tmp_path)

    assert single is False and "占用者未知" in detail


def test_真起第二个进程也会撞上(tmp_path, request):
    """跨进程才算真现场。

    同进程另开 fd 已经是独立的 open file description，但这一条补的是最硬的
    证据：另起一个 Python 进程按同样的方式拿锁，本进程必须报多副本——
    否则整个检测在生产里等于没接上（比如哪天换成正则/环境变量判断就会这样）。
    """
    root = str(request.config.rootpath)
    script = (
        "import sys, time\n"
        "from bot import single_instance\n"
        "assert single_instance.acquire(sys.argv[1])[0] is True\n"
        "print('HOLDING', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=root,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert "HOLDING" in (proc.stdout.readline() or ""), "子进程没能拿到锁，用例前提不成立"
        single, detail = single_instance.acquire(tmp_path)
    finally:
        proc.kill()
        proc.wait(timeout=10)

    assert single is False and "另一份服务在运行" in detail


def test_锁释放后能重新拿到(tmp_path):
    """另一份退出（锁跟着 fd 消失）之后，本进程要能正常拿到——否则一次崩溃
    之后每次重启都报多副本，警告很快就没人看了。"""
    path = single_instance.lock_path(tmp_path)
    with _foreign_holder(path):
        assert single_instance.acquire(tmp_path)[0] is False

    single_instance.reset()
    assert single_instance.acquire(tmp_path) == (True, "")


# ---------- 判断不了的时候 ----------

def test_锁文件建不了就闭嘴(tmp_path):
    """只读卷 / 权限不对：判断不了就当没问题。漏报一次可以，误报天天有不行。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("我是一个文件，不是目录", encoding="utf-8")

    single, detail = single_instance.acquire(blocked)

    assert single is True and detail == ""


def test_建不了锁文件也不打多副本警告(tmp_path, caplog):
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="bot.single_instance"):
        single_instance.warn_if_duplicate(blocked)

    assert not [r for r in caplog.records if "另一份服务" in r.getMessage()]


# ---------- 检测不许改变行为 ----------

class _StubScheduler:
    """替身调度器：只登记 job，不起线程"""

    def __init__(self, timezone=None, **kwargs):
        self.jobs: dict[str, object] = {}
        self.started = False

    def add_job(self, func, trigger, **kwargs):
        self.jobs[kwargs["id"]] = func

    def start(self):
        self.started = True

    def shutdown(self, wait=True):
        pass


def test_多副本不许拦住调度器启动(tmp_path, monkeypatch):
    """整件事的分寸就在这一条：发现了多副本，也只是多一行日志。

    改动一旦让"多副本 → 不启动/不推日报"，这里必须变红——
    那等于把一次重复推送换成了彻底不推。
    """
    from server.app import config as config_mod

    created: list[_StubScheduler] = []

    class Factory(_StubScheduler):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            created.append(self)

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        "apscheduler.schedulers.background.BackgroundScheduler", Factory
    )
    monkeypatch.setattr(scheduler_mod, "_scheduler", None)

    with _foreign_holder(single_instance.lock_path(tmp_path)):
        scheduler_mod.start_scheduler(app=None)

    try:
        assert len(created) == 1, "检测到多副本也不能不建调度器"
        sched = created[0]
        assert sched.started is True
        assert set(sched.jobs) == {"daily_report", "report_patrol", "nightly_maintenance"}
    finally:
        scheduler_mod.stop_scheduler()


# ---------- 仪表盘 ----------

def test_仪表盘服务行在多副本时变黄(tmp_path):
    from server.app import status

    with _foreign_holder(single_instance.lock_path(tmp_path)):
        single_instance.acquire(tmp_path)
        row = status._service_row()

    assert row["status"] == status.STATUS_WARN, "多副本是提醒不是事故，别用红点"
    assert "另一份服务在运行" in row["detail"]


def test_仪表盘服务行单副本时只说跑了多久(tmp_path):
    from server.app import status

    single_instance.acquire(tmp_path)
    row = status._service_row()

    assert row["status"] == status.STATUS_OK
    assert "另一份服务" not in row["detail"]
