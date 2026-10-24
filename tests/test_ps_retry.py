"""core/ps_retry：连接预算、错误分类、PS 进程探测。

这几条都是"线上才会踩"的：错误码写成无符号十六进制、pywin32 给的是有符号负数，
一旦没归一化，所有码表都命中不了 —— 表现就是"PS 正在启动"被当成未知错误立刻抛出，
员工第一次点生成照样失败，而且错误文案还变成一句莫名其妙的 COM 报错。
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

pywintypes = pytest.importorskip("pywintypes", reason="pywin32 仅 Windows 可用")

import core.ps_retry as r


def _err(hresult: int, excepinfo=None):
    return pywintypes.com_error(hresult, "msg", excepinfo, None)


# ---------- HRESULT 归一化 ----------

@pytest.mark.parametrize(
    "signed, unsigned",
    [
        (-2146959355, 0x80080005),  # CO_E_SERVER_EXEC_FAILURE
        (-2147221164, 0x80040154),  # REGDB_E_CLASSNOTREG
        (-2147023174, 0x800706BA),  # RPC_S_SERVER_UNAVAILABLE
        (0x80080005, 0x80080005),  # 已经无符号的原样返回
    ],
)
def test_error_code_normalizes_signed_and_unsigned(signed, unsigned):
    assert r.error_code(_err(signed)) == unsigned


def test_error_code_without_hresult_or_excepinfo_is_none():
    """回归：excepinfo=None 时 None[0] 曾抛 TypeError 从重试循环里漏出"""
    assert r.error_code(pywintypes.com_error(0, "no excepinfo", None, None)) is None


def test_error_code_reads_excepinfo_when_hresult_missing():
    err = SimpleNamespace(hresult=None, excepinfo=(-2147221164, "desc", None, None, None))
    assert r.error_code(err) == 0x80040154


def test_error_code_survives_garbage_excepinfo():
    assert r.error_code(SimpleNamespace(hresult=None, excepinfo=42)) is None


# ---------- 分类 ----------

def test_startup_error_is_retryable():
    assert r.classify(_err(-2146959355)) == r.RETRY


@pytest.mark.parametrize("code", [0x8001010A, 0x80010001, 0x800401E3, 0x800706BE, 0x80080008])
def test_busy_and_not_yet_registered_are_retryable(code):
    assert r.classify(_err(code)) == r.RETRY


@pytest.mark.parametrize("code", [0x80040154, 0x800706BA, 0x80070002, 0x8007007E])
def test_install_problems_are_fatal(code):
    assert r.classify(_err(code)) == r.FATAL
    assert r.fatal_message(_err(code)), "致命错误必须带可执行文案"


def test_unknown_com_error_is_not_retried():
    """不认识的错误码不许当"PS 在启动"重试——那会把真正的 bug 藏 60 秒"""
    assert r.classify(_err(0x8000FFFF)) == r.UNKNOWN
    assert r.fatal_message(_err(0x8000FFFF)) == ""


def test_non_com_errors_are_unknown():
    assert r.classify(ValueError("boom")) == r.UNKNOWN
    assert r.classify(KeyError("x")) == r.UNKNOWN


# ---------- 预算与退避 ----------

def test_budget_is_split_by_cold_start():
    assert r.budget_for(True) > r.budget_for(False)
    assert r.COLD_START_BUDGET_S >= 45, "实测冷启 15~40 秒，预算必须盖住上限"
    assert r.WARM_BUDGET_S >= 5


def test_retry_delay_grows_then_caps():
    delays = [r.retry_delay(i) for i in range(8)]
    assert delays[0] == pytest.approx(0.5)
    assert delays == sorted(delays), "退避必须单调不减"
    assert max(delays) == pytest.approx(r._MAX_DELAY_S)
    assert all(d <= r._MAX_DELAY_S for d in delays)


def test_retry_delay_first_attempt_is_short():
    """第一次重试要快：多数机器 1 秒内 PS 就绪，等 2 秒纯浪费"""
    assert r.retry_delay(0) < 1.0


# ---------- PS 进程探测 ----------

def test_is_running_true_when_tasklist_lists_photoshop(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout="Photoshop.exe\t1234\tConsole\t1\t100 K"),
    )
    assert r.is_photoshop_running() is True


def test_is_running_false_when_tasklist_says_nothing_matches(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout="信息: 没有运行的任务匹配指定标准。"),
    )
    assert r.is_photoshop_running() is False


def test_is_running_falls_back_to_com_when_tasklist_unavailable(monkeypatch):
    """tasklist 被安全软件拦掉时，用运行对象表兜底"""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("denied")))
    active = SimpleNamespace(client=SimpleNamespace(GetActiveObject=lambda progid: object()))
    missing = SimpleNamespace(
        client=SimpleNamespace(
            GetActiveObject=lambda progid: (_ for _ in ()).throw(RuntimeError("not found"))
        )
    )

    assert r.is_photoshop_running(active) is True
    assert r.is_photoshop_running(missing) is False


def test_is_running_defaults_to_cold_when_nothing_can_tell(monkeypatch):
    """两条路都问不出来时按"冷启动"处理：宁可多给预算，也不要让第一次生成必然失败"""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("x")))
    assert r.is_photoshop_running(None) is False


# ---------- 预启动 ----------

def test_prelaunch_starts_configured_exe(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    exe = tmp_path / "Photoshop.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append(args))

    assert r.prelaunch(exe) is True
    assert calls == [[str(exe)]]


def test_prelaunch_skips_missing_path(monkeypatch, tmp_path):
    """配置里的路径写错了不要炸——让 COM 自己再试一次就好"""
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不该调用"))
    )
    assert r.prelaunch(tmp_path / "missing.exe") is False
    assert r.prelaunch("") is False


def test_prelaunch_survives_popen_failure(monkeypatch, tmp_path):
    exe = tmp_path / "Photoshop.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(OSError("拒绝访问"))
    )
    assert r.prelaunch(exe) is False


def test_prelaunch_accepts_str_path(monkeypatch, tmp_path):
    calls = []
    exe = tmp_path / "Photoshop.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append(args))
    assert r.prelaunch(str(exe)) is True
    assert calls == [[str(exe)]]
