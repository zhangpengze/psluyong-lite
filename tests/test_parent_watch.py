"""父进程看门狗

这层东西存在的理由很具体：Tauri 被任务管理器强杀后，Windows 不给子进程任何
通知，sidecar 继续跑、继续占着 8765。下次启动的新 sidecar 绑不上端口、异常栈
进了 Stdio::null()（打包后 stdout/stderr 就是黑洞），而前端照样连得上 8765——
连到的是那个**僵尸**：busy 永远 true、配置是上一轮的、日志里一个字都没有。
员工看到的是"软件启动了，但按钮一直说忙"。

worker 盯 sidecar 同理：Rust 退出时只 kill 一级子进程，worker 是孙进程，成了
孤儿还抱着 ps.lock 和 PS 的 COM。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

import pytest

from client.sidecar import parent_watch


@pytest.fixture()
def no_env(monkeypatch):
    monkeypatch.delenv(parent_watch.PARENT_PID_ENV, raising=False)


def _dead_pid() -> int:
    """一个确定已经退出的进程 pid"""
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=30)
    return proc.pid


# ========== 读环境变量 ==========


def test_pid_from_env_absent(no_env):
    """没配 = 不启用。手动跑 sidecar、跑测试都不该被看门狗影响"""
    assert parent_watch.pid_from_env() is None


@pytest.mark.parametrize("raw", ["", "   ", "abc", "12x", "-5", "0"])
def test_pid_from_env_rejects_garbage(monkeypatch, raw):
    monkeypatch.setenv(parent_watch.PARENT_PID_ENV, raw)
    assert parent_watch.pid_from_env() is None


def test_pid_from_env_warns_on_garbage(monkeypatch, caplog):
    """配坏了要说一声：悄悄不启用，等于这条防线从来没生效过"""
    monkeypatch.setenv(parent_watch.PARENT_PID_ENV, "tauri")
    with caplog.at_level(logging.WARNING):
        assert parent_watch.pid_from_env() is None
    assert any("不是数字" in r.message for r in caplog.records)


def test_pid_from_env_ok(monkeypatch):
    monkeypatch.setenv(parent_watch.PARENT_PID_ENV, "4321")
    assert parent_watch.pid_from_env() == 4321


def test_pid_from_env_trims_spaces(monkeypatch):
    monkeypatch.setenv(parent_watch.PARENT_PID_ENV, "  4321  ")
    assert parent_watch.pid_from_env() == 4321


# ========== 判活 ==========


def test_alive_self():
    assert parent_watch.alive(os.getpid()) is True


def test_alive_reports_exited_process():
    assert parent_watch.alive(_dead_pid()) is False


@pytest.mark.parametrize("pid", [0, -1, -9999])
def test_alive_rejects_nonsense_pids(pid):
    """0/负数喂给 Windows API 会被当成"当前进程组"之类，必须自己先挡住"""
    assert parent_watch.alive(pid) is False


def test_alive_does_not_kill_the_target():
    """Windows 上 os.kill(pid, 0) 会真的 TerminateProcess —— 这里守住"只查不杀"

    如果哪天有人把实现改成 os.kill(pid, 0)，这条用例会连自己一起杀掉，
    所以判活用的是 OpenProcess + WaitForSingleObject。
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert parent_watch.alive(proc.pid) is True
        time.sleep(0.2)
        assert proc.poll() is None, "判活把目标进程杀了"
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ========== 看门狗线程 ==========


def test_start_without_env_returns_none(no_env):
    assert parent_watch.start() is None


def test_watchdog_exits_when_parent_gone():
    """父进程没了 -> 收尾 + 退出。exit_fn 换掉，否则会把 pytest 一起 os._exit"""
    exits: list[int] = []
    lost: list[bool] = []

    thread = parent_watch.start(
        on_lost=lambda: lost.append(True),
        interval=0.01,
        pid=_dead_pid(),
        exit_fn=exits.append,
    )
    assert thread is not None
    thread.join(timeout=5)

    assert exits == [0], "必须自己退出，否则会占着端口变成僵尸"
    assert lost == [True], "退出前的收尾（收子进程）要跑到"
    assert not thread.is_alive()


def test_watchdog_keeps_quiet_while_parent_alive():
    """父进程还在就别乱退：正常跑着的 sidecar 被自己看门狗杀了才是灾难"""
    exits: list[int] = []

    thread = parent_watch.start(interval=0.01, pid=os.getpid(), exit_fn=exits.append)
    assert thread is not None
    time.sleep(0.15)

    assert exits == []
    assert thread.is_alive()


def test_on_lost_failure_still_exits():
    """收尾动作自己炸了也必须退：留在原地就是僵尸"""
    exits: list[int] = []

    def boom() -> None:
        raise RuntimeError("收尾失败")

    thread = parent_watch.start(
        on_lost=boom, interval=0.01, pid=_dead_pid(), exit_fn=exits.append
    )
    thread.join(timeout=5)

    assert exits == [0]
