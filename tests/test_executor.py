"""worker 子进程与执行管理器测试

全程不依赖 Photoshop：用"模板文件不存在"这条最短的失败路径验证
worker 能把终态落盘；用真实子进程验证 sidecar<->worker 的文件协议。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from client.sidecar.executor import BusyError, Executor
from client.sidecar.worker import read_json, run, write_json_atomic

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# worker 用 msvcrt 做 ps.lock 进程锁并调 Photoshop COM，产品只跑 Windows
# （见 client/sidecar/worker.py 的注释）。POSIX 上真子进程会在 worker 里以
# "No module named 'msvcrt'" 失败，这些用例整体跳过；纯文件协议逻辑仍在本文件
# 里跨平台跑（原子写、撕裂写、状态未知等）。
WIN_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="sidecar worker 依赖 msvcrt/PS COM，仅 Windows 可跑",
)


def _make_params(task_dir: Path, **overrides) -> dict:
    params = {
        "template_path": str(task_dir / "不存在.psd"),  # 最短失败路径，不需要 PS
        "author": "张三",
        "title": "测试标题",
        "output_dir": str(task_dir / "out"),
        "jpg_quality": 10,
        "author_placeholder": "{{作者}}",
        "title_placeholder": "{{标题}}",
        "ps_path": "",
    }
    params.update(overrides)
    return params


def test_atomic_write_is_readable(tmp_path: Path):
    target = tmp_path / "status.json"
    write_json_atomic(target, {"state": "done"})
    assert read_json(target) == {"state": "done"}
    # 原子写不能留临时文件
    assert not list(tmp_path.glob("*.tmp"))


@WIN_ONLY
def test_worker_writes_final_state_without_ps(tmp_path: Path):
    """无 PS 环境：run_export 走到"模板不存在"失败分支，终态必须落盘"""
    write_json_atomic(tmp_path / "params.json", _make_params(tmp_path))
    run(tmp_path)
    status = read_json(tmp_path / "status.json")
    assert status is not None, "worker 必须写终态，哪怕失败"
    assert status["state"] == "done"
    assert status["success"] is False
    assert "模板文件不存在" in status["message"]


def test_worker_missing_params_writes_error(tmp_path: Path):
    run(tmp_path)  # 没有 params.json
    status = read_json(tmp_path / "status.json")
    assert status is not None and status["state"] == "error"


@WIN_ONLY
def test_executor_end_to_end_with_real_subprocess(tmp_path: Path):
    """真实子进程拉起 worker，验证 sidecar<->worker 文件协议全链路"""
    ex = Executor()
    params = _make_params(tmp_path)
    task_id = ex.start(params)

    deadline = time.monotonic() + 30
    status = {}
    while time.monotonic() < deadline:
        status = ex.status(task_id)
        if status.get("finished"):
            break
        assert status["state"] == "running"
        time.sleep(0.3)

    assert status["finished"], f"30 秒内未出终态: {status}"
    assert status["success"] is False
    assert "模板文件不存在" in status["message"]
    # 终态读出后锁必须已释放，可以立刻提交下一个任务
    assert ex.running_task() is None


def test_executor_rejects_concurrent_start(tmp_path: Path):
    ex = Executor()
    # 直接占住锁，模拟上一个任务还在跑
    assert ex._held.acquire(blocking=False)
    with pytest.raises(BusyError):
        ex.start(_make_params(tmp_path))


def test_executor_reports_uncertain_on_crash(tmp_path: Path):
    """worker 退出但没写终态：必须报"结果未知"，绝不能假装成功"""
    ex = Executor()
    task_id = ex.start(_make_params(tmp_path))
    task = ex._tasks[task_id]

    # 模拟"进程退了、终态没写"：杀掉真 worker，删掉它写的 status.json
    task["popen"].kill()
    task["popen"].wait(timeout=10)
    (Path(task["dir"]) / "status.json").unlink(missing_ok=True)

    status = ex.status(task_id)
    assert status["state"] == "uncertain"
    assert status["finished"] is True
    assert status["success"] is False
    assert "结果未知" in status["message"]
    assert ex.running_task() is None


@WIN_ONLY
def test_status_from_disk_after_release(tmp_path: Path):
    """reaper 先于客户端轮询释放任务后，终态必须仍能从磁盘读回。

    真实 race（50 条批量测试踩到）：worker 退出后，reaper 的 5s 巡查
    先于客户端 1s 轮询释放了任务，之后查询落到 unknown，上层报
    "生成失败"——图已经生出来了、记录却没上报，员工白干一次。
    """
    ex = Executor()
    task_id = ex.start(_make_params(tmp_path))
    task = ex._tasks[task_id]

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if read_json(task["dir"] / "status.json") is not None:
            break
        time.sleep(0.2)
    assert read_json(task["dir"] / "status.json") is not None, "worker 应已写终态"

    # 模拟 reaper 抢在客户端前面释放
    ex._release(task_id)
    assert ex.running_task() is None

    status = ex.status(task_id)
    assert status["finished"] is True
    assert status["success"] is False
    # 必须是 worker 的真实失败原因，而不是假报的"生成失败"
    assert "模板文件不存在" in status["message"]


def test_status_unknown_only_when_no_terminal_file(tmp_path: Path):
    """磁盘上也没有终态文件时，才允许报 unknown"""
    ex = Executor()
    status = ex.status("从不存在的任务id")
    assert status == {"state": "unknown", "finished": True}


@WIN_ONLY
def test_no_lock_leak_under_reaper_poll_race(tmp_path: Path):
    """reaper 与客户端轮询竞速多轮，执行器锁不允许泄漏。

    背景：50 条批量真实测试中偶发一次 409「忙」——用本测试同款压力
    验证证明产品代码获取/释放是平衡的（那次是环境层假象）。守住回归：
    终态已交付给客户端后，下一次 start 必须立刻成功，绝不能报忙。
    """
    ex = Executor()

    def params(i: int) -> dict:
        p = _make_params(tmp_path)
        p["author"] = f"压测{i}"
        return p

    def wait_terminal(task_dir: Path) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if read_json(task_dir / "status.json") is not None:
                return
            time.sleep(0.05)
        raise AssertionError("30 秒内未出终态")

    for i in range(6):
        task_id = ex.start(params(i))
        wait_terminal(ex._tasks[task_id]["dir"])
        if i % 2 == 0:
            ex._release(task_id)  # reaper 先释放 -> 客户端随后走磁盘回退路径
        status = ex.status(task_id)
        assert status["finished"] is True
        assert "模板文件不存在" in status["message"]

        # 终态交付后必须能立刻启动下一个，不允许 BusyError
        next_id = ex.start(params(i + 100))
        wait_terminal(ex._tasks[next_id]["dir"])
        ex._release(next_id)


def test_status_file_survives_torn_write(tmp_path: Path):
    """params.json 损坏时读回 None，worker 报 error 而不是拿半截参数去跑 PS"""
    bad = tmp_path / "params.json"
    bad.write_text('{"template_path": "截断', encoding="utf-8")
    assert read_json(bad) is None


def test_cancel_writes_cancelled_state_and_kills_worker(tmp_path: Path, monkeypatch):
    """取消必须落成确定的终态，而不是"结果未知"。

    顺序错了（先杀进程再写终态）的话 status() 会先看到进程消失、读不到
    status.json，判成 uncertain，员工被通知"可能已经生成了一张图"，
    然后跑去输出目录翻文件——他只是点了个取消。
    """
    ex = Executor()
    spawned: list[subprocess.Popen] = []

    def never_finishing(self, task_dir):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spawned.append(proc)
        return proc

    monkeypatch.setattr(Executor, "_spawn_worker", never_finishing)

    task_id = ex.start(_make_params(tmp_path))
    ok, message = ex.cancel(task_id)
    assert ok, message

    # 进程真的被杀掉了
    assert spawned and spawned[0].wait(timeout=10) is not None

    status = ex.status(task_id)
    assert status["state"] == "cancelled"
    assert status["finished"] is True
    assert status["success"] is False
    assert "已取消" in status["message"]
    # 锁必须释放，员工能立刻重试
    assert ex.running_task() is None


def test_cancel_unknown_task_is_soft_failure(tmp_path: Path):
    """取消一个已经结束的任务：返回 False 而不是抛异常"""
    ex = Executor()
    ok, message = ex.cancel("不存在的-task-id")
    assert ok is False
    assert message


def test_cancel_then_start_again(tmp_path: Path):
    """取消之后必须能立刻再开一个任务，否则员工就卡死了"""
    ex = Executor()

    import client.sidecar.executor as executor_mod

    real_spawn = executor_mod.Executor._spawn_worker

    def slow_then_real(self, task_dir):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    executor_mod.Executor._spawn_worker = slow_then_real
    try:
        task_id = ex.start(_make_params(tmp_path))
        assert ex.cancel(task_id)[0] is True
        assert ex.running_task() is None
    finally:
        executor_mod.Executor._spawn_worker = real_spawn

    # 取消后重新提交：这次走真 worker（模板不存在 → 失败终态），
    # 只要没抛 BusyError 就说明锁已经放干净了
    second = ex.start(_make_params(tmp_path))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ex.status(second).get("finished"):
            return
        time.sleep(0.2)
    raise AssertionError("取消后重新提交的任务没有出终态")
