"""Photoshop 自动识别的测试。

只测纯逻辑和"注入假候选"的决策：真机探测要碰注册表，
那部分靠 detect_with 把采集函数换掉来模拟（不需要真装 PS）。
候选一律用临时目录造出来——开发机上装着什么不该影响测试结果
（真踩过：拿本机 CS6 的真实路径当候选，换台机器测试就红）。
"""

import sys

import pytest

from client.sidecar.ps_detect import (
    detect_with,
    is_photoshop_exe,
    pick_best,
    version_sort_key,
    _year_from_name,
)


def _fake_exe(tmp_path, dirname: str) -> str:
    """造一个假的 Photoshop.exe（文件版本信息读不到，正好覆盖降级路径）。"""
    d = tmp_path / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "Photoshop.exe").write_bytes(b"x")
    return str(d / "Photoshop.exe")


def _collectors(*groups):
    """按真实来源名构造候选组：顺序 = 注册表、安装信息、目录扫描。"""
    names = ("app-paths", "uninstall", "scan")
    return [(names[i], lambda g=g: g) for i, g in enumerate(groups)]


# ========== 纯函数 ==========


def test_year_from_name():
    """老版本号排得进序列：CS6 必须早于 2024，否则多版本机器会挑错。"""
    assert _year_from_name("Adobe Photoshop 2024") == 2024
    assert _year_from_name("Adobe Photoshop CC 2015") == 2015
    assert _year_from_name("Adobe Photoshop CS6 (64 Bit)") == 2012
    assert _year_from_name("Adobe Photoshop CC") == 2013
    assert _year_from_name("别的软件") == 0


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="断言的是 Windows 路径语义（C:\\ 反斜杠与 .exe 后缀），POSIX 上 Path 切不开",
)
def test_is_photoshop_exe():
    assert is_photoshop_exe(r"C:\Program Files\Adobe\Adobe Photoshop 2024\Photoshop.exe")
    assert not is_photoshop_exe(r"C:\Program Files\Adobe\Photoshop.dll")
    assert not is_photoshop_exe("")


def test_version_sort_key_版本位数不齐也能比():
    assert version_sort_key(r"C:\x\Photoshop.exe") >= (0, 0, 0)


# ========== 挑最优候选 ==========


def test_pick_best_取版本最高(tmp_path, monkeypatch):
    """CS6 + 2024 并存时选 2024：COM ProgID 解析到的通常也是最新版，
    两者一致员工才看得出连的是同一个。"""
    old = _fake_exe(tmp_path, "Adobe Photoshop CS6 (64 Bit)")
    new = _fake_exe(tmp_path, "Adobe Photoshop 2024")
    monkeypatch.setattr(
        "client.sidecar.ps_detect._file_version",
        lambda p: "13.0.0" if "CS6" in p else "25.0.0",
    )
    assert pick_best([old, new]) == new


def test_pick_best_文件版本读不到时按目录名(tmp_path, monkeypatch):
    """精简版/绿色版读不到文件版本：至少还能按目录名里的年份排。"""
    old = _fake_exe(tmp_path, "Adobe Photoshop CS6")
    new = _fake_exe(tmp_path, "Adobe Photoshop 2024")
    monkeypatch.setattr("client.sidecar.ps_detect._file_version", lambda p: "")
    assert pick_best([old, new]) == new


def test_pick_best_候选全不存在时返回空():
    assert pick_best([r"C:\nope\Photoshop.exe", ""]) == ""


# ========== 识别顺序 ==========


def test_识别顺序_注册表优先于安装信息和扫描(tmp_path):
    cs6 = _fake_exe(tmp_path, "Adobe Photoshop CS6 (64 Bit)")
    info = detect_with(_collectors([cs6], [], [cs6]))
    assert info.path == cs6
    assert info.source == "app-paths"


def test_注册表没有才落到安装信息(tmp_path):
    ps2024 = _fake_exe(tmp_path, "Adobe Photoshop 2024")
    info = detect_with(_collectors([], [ps2024], []))
    assert info.path == ps2024
    assert info.source == "uninstall"


def test_全都没有才扫目录(tmp_path):
    cs6 = _fake_exe(tmp_path, "Adobe Photoshop CS6")
    info = detect_with(_collectors([], [], [cs6]))
    assert info.path == cs6
    assert info.source == "scan"


def test_三处都没有_返回未识别():
    info = detect_with(_collectors([], [], []))
    assert not info.found
    assert info.path == ""
    assert info.source == "none"
    assert info.to_dict()["source_label"] == "未识别"


# ========== 手填路径 ==========


def test_手填路径优先_且不碰系统探测(tmp_path):
    """员工明确指定时以他为准——他可能就是想驱动旧版。"""
    manual = _fake_exe(tmp_path, "Adobe Photoshop 2021")
    called = []
    cols = [(name, lambda: called.append(name) or []) for name in ("app-paths", "uninstall", "scan")]
    info = detect_with(cols, explicit=manual)
    assert info.path == manual
    assert info.source == "config"
    assert called == [], "手填了就不该再去探测系统"


def test_手填路径不存在_自动识别顶上并标记出来(tmp_path):
    """失效的手填值不该卡住生成，但也不能装作没事：
    用自动识别的结果，同时把"你填的路径不存在"带回界面。"""
    cs6 = _fake_exe(tmp_path, "Adobe Photoshop CS6 (64 Bit)")
    info = detect_with(_collectors([cs6]), explicit=r"C:\nope\Photoshop.exe")
    assert info.found
    assert info.path == cs6
    assert info.source == "app-paths"
    assert info.override_missing is True


def test_手填路径不存在且本机也没装():
    info = detect_with(_collectors([]), explicit=r"C:\nope\Photoshop.exe")
    assert not info.found
    assert info.override_missing is True


# ========== 对外形状 ==========


def test_to_dict_给人看的字段(tmp_path):
    cs6 = _fake_exe(tmp_path, "Adobe Photoshop CS6 (64 Bit)")
    d = detect_with(_collectors([cs6])).to_dict()
    assert d["found"] is True
    assert d["source_label"] == "系统注册表识别"
    assert d["path"] == cs6
    assert d["override_missing"] is False
