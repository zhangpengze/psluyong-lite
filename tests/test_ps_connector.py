"""core/ps_connector 测试：连接建立 / COM 错误重试 / 文本替换 / 导出的可测边界

不依赖真 Photoshop：win32com/pythoncom 全部用替身注入。
覆盖三类边界：
  - COM 可用时的行为契约（重试次数、错误码人话、调用序列）
  - COM 不可用时的降级（ensure_com_available 快速失败 + 非 Windows
    import 不炸——后者用子进程模拟无 pywin32 环境实测）
  - 文本匹配/替换/导出的纯逻辑（换行归一、字面量反向引用、落盘选项）
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

# pywin32 由 requirements 用 sys_platform 标记只在 Windows 安装；本文件的用例全部
# 围绕 com_error 对象构造与 COM 替身注入，非 Windows 平台整体跳过——否则 Linux CI
# 会在收集期 ImportError（pytest 退出码 2，整个 job 直接红）。
pywintypes = pytest.importorskip(
    "pywintypes", reason="pywin32 仅 Windows 可用：COM 相关用例在非 Windows 平台跳过"
)

import core.ps_connector as m
from core.ps_connector import PSConnector, ensure_com_available

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- 帮助 ----------

def _com_error(hresult: int, desc: str = "") -> pywintypes.com_error:
    return pywintypes.com_error(hresult, "system message", (None, desc, None, 0, None), None)


def _fake_win32com(dispatch):
    return SimpleNamespace(client=SimpleNamespace(Dispatch=dispatch))


@pytest.fixture()
def no_sleep(monkeypatch):
    """记录 sleep 调用并跳过真实等待（connect/错误处理里有 1s/2s 重试间隔）"""
    slept: list[int] = []
    monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
    return slept


class FakeApp:
    def __init__(self, visible=True):
        self.Visible = visible
        self.DisplayDialogs = None
        self.opened: list[str] = []
        self.open_error: Exception | None = None

    def Open(self, path: str):
        if self.open_error is not None:
            raise self.open_error
        self.opened.append(path)
        return f"doc:{path}"


# ========== 环境降级 ==========

def test_ensure_com_available_raises_without_com(monkeypatch):
    monkeypatch.setattr(m, "COM_AVAILABLE", False)
    with pytest.raises(RuntimeError) as ei:
        ensure_com_available()
    msg = str(ei.value)
    assert "pywin32" in msg and "Windows" in msg
    # 触发点是 sys.platform，必须如实反映当前环境
    assert sys.platform in msg


def test_ensure_com_available_passes_with_com():
    assert ensure_com_available() is None  # 本机 COM 可用时静默通过


def test_import_without_pywin32_does_not_crash_and_degrades():
    """非 Windows / 没装 pywin32：core.ps_connector 必须能 import，
    COM_AVAILABLE=False，connect 给出带安装指引的快速失败。
    用子进程 + 污染 sys.modules 模拟，不污染本进程已导入的模块。"""
    script = textwrap.dedent(
        """
        import sys
        for name in ("pythoncom", "pywintypes", "win32com", "win32com.client"):
            sys.modules[name] = None  # None 条目让后续 import 抛 ImportError
        import core.ps_connector as m

        assert m.COM_AVAILABLE is False, "COM 可用性标志必须为 False"
        assert m.COM_ERRORS == (m._NeverRaisedError,), "无真实 com_error 类型时用占位异常"
        assert m.pythoncom is None and m.win32com is None
        try:
            m.ensure_com_available()
        except RuntimeError as e:
            assert "pywin32" in str(e)
        else:
            raise AssertionError("ensure_com_available 应该抛 RuntimeError")
        try:
            m.PSConnector().connect("")
        except RuntimeError as e:
            assert "pywin32" in str(e)
        else:
            raise AssertionError("无 COM 时 connect 应该快速失败")
        print("NONWINDOWS_IMPORT_OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"子进程失败:\n{proc.stdout}\n{proc.stderr}"
    assert "NONWINDOWS_IMPORT_OK" in proc.stdout


# ========== connect ==========

def test_connect_success_initializes_com_and_marks_visible(monkeypatch, no_sleep):
    coinit: list[int] = []
    app = FakeApp(visible=False)
    monkeypatch.setattr(m, "pythoncom", SimpleNamespace(CoInitialize=lambda: coinit.append(1)))
    monkeypatch.setattr(m, "win32com", _fake_win32com(lambda progid: app))

    conn = PSConnector()
    conn.connect("C:/PS/Photoshop.exe")

    assert conn.app is app
    assert coinit == [1], "未初始化 COM 的线程必须先 CoInitialize，否则 Dispatch 必炸"
    assert app.Visible is True, "后台拉起的 PS 必须置为可见，否则员工看不到卡在哪"


def test_connect_coinitialize_failure_is_swallowed(monkeypatch, no_sleep):
    def boom():
        raise RuntimeError("already initialized?")

    app = FakeApp(visible=True)
    monkeypatch.setattr(m, "pythoncom", SimpleNamespace(CoInitialize=boom))
    monkeypatch.setattr(m, "win32com", _fake_win32com(lambda progid: app))

    conn = PSConnector()
    conn.connect("")  # CoInitialize 失败不能阻断连接
    assert conn.app is app


def test_connect_visible_read_error_does_not_block(monkeypatch, no_sleep):
    class AppBadVisible:
        @property
        def Visible(self):
            raise _com_error(-2147352567, "query denied")

    app = AppBadVisible()
    monkeypatch.setattr(m, "pythoncom", SimpleNamespace(CoInitialize=lambda: None))
    monkeypatch.setattr(m, "win32com", _fake_win32com(lambda progid: app))

    conn = PSConnector()
    conn.connect("")
    assert conn.app is app


def test_connect_retries_three_times_then_friendly_error(monkeypatch, no_sleep):
    attempts: list[int] = []

    def failing_dispatch(progid):
        attempts.append(1)
        raise ValueError("dispatch failed")

    # 打桩错误处理器：模拟"可重试"的错误，验证 3 次重试后给员工的最终指引
    monkeypatch.setattr(PSConnector, "_handle_com_error", lambda self, exc, a, n: None)
    monkeypatch.setattr(m, "pythoncom", SimpleNamespace(CoInitialize=lambda: None))
    monkeypatch.setattr(m, "win32com", _fake_win32com(failing_dispatch))

    conn = PSConnector()
    with pytest.raises(RuntimeError) as ei:
        conn.connect("")
    assert len(attempts) == 3, "必须重试满 3 次"
    msg = str(ei.value)
    assert "已重试 3 次" in msg
    assert "管理员身份" in msg, "最终报错必须给员工可执行的下一步"
    assert no_sleep == [1, 1, 1], "每次重试之间要等 1 秒（含最后一次尝试后）"


def test_connect_generic_error_propagates_immediately(monkeypatch, no_sleep):
    """真实错误处理器对非 COM 错误原样上抛——不吞不改，调用方能看到根因"""
    monkeypatch.setattr(m, "pythoncom", SimpleNamespace(CoInitialize=lambda: None))
    monkeypatch.setattr(m, "win32com", _fake_win32com(lambda progid: (_ for _ in ()).throw(ValueError("boom"))))

    conn = PSConnector()
    with pytest.raises(ValueError, match="boom"):
        conn.connect("")
    assert no_sleep == [], "非 COM 错误不该走重试等待"


def test_connect_runtime_error_from_handler_propagates(monkeypatch, no_sleep):
    """handler 判定为业务语义错误（如组件未注册）时必须直接上抛，
    不许被重试循环吞成「连接失败」泛化文案"""
    monkeypatch.setattr(m, "pythoncom", SimpleNamespace(CoInitialize=lambda: None))
    monkeypatch.setattr(
        m,
        "win32com",
        _fake_win32com(lambda progid: (_ for _ in ()).throw(_com_error(-2147221164))),
    )

    conn = PSConnector()
    with pytest.raises(RuntimeError) as ei:
        conn.connect("")
    assert "COM 组件未注册" in str(ei.value)


# ========== _handle_com_error 错误码分流 ==========

def test_unregistered_com_component_message():
    conn = PSConnector()
    with pytest.raises(RuntimeError) as ei:
        conn._handle_com_error(_com_error(-2147221164), attempt=0, max_retries=3)
    assert "COM 组件未注册" in str(ei.value)


def test_startup_error_retries_then_gives_up(no_sleep):
    conn = PSConnector()
    # 前两次：等待重试（返回而不是抛）
    assert conn._handle_com_error(_com_error(-2146959355), attempt=0, max_retries=3) is None
    assert conn._handle_com_error(_com_error(-2146959355), attempt=1, max_retries=3) is None
    assert no_sleep == [2, 2], "PS 启动等待间隔 2 秒"
    # 最后一次：放弃并给员工明确指引
    with pytest.raises(RuntimeError) as ei:
        conn._handle_com_error(_com_error(-2146959355), attempt=2, max_retries=3)
    assert "无法启动 Photoshop，请手动启动" in str(ei.value)


def test_connection_lost_error_message():
    conn = PSConnector()
    with pytest.raises(RuntimeError) as ei:
        conn._handle_com_error(_com_error(-2147023174), attempt=0, max_retries=3)
    assert "连接已断开" in str(ei.value)


def test_non_com_error_reraised_as_is():
    conn = PSConnector()
    err = ValueError("not com")
    with pytest.raises(ValueError, match="not com"):
        conn._handle_com_error(err, attempt=0, max_retries=3)


def test_error_with_no_hresult_and_no_excepinfo_does_not_typeerror():
    """回归：hresult=0 且 excepinfo=None 曾让 None[0] 抛 TypeError 从
    重试逻辑里漏出——必须原样抛 com_error，而不是 TypeError。"""
    conn = PSConnector()
    with pytest.raises(pywintypes.com_error):
        conn._handle_com_error(_com_error_with_none_excepinfo(), attempt=0, max_retries=3)


def _com_error_with_none_excepinfo() -> pywintypes.com_error:
    return pywintypes.com_error(0, "no excepinfo", None, None)


# ========== open / dialogs / close ==========

def test_open_document_without_connection():
    conn = PSConnector()
    with pytest.raises(RuntimeError, match="Photoshop COM 未连接"):
        conn.open_document(Path("t.psd"))


def test_open_document_resolves_absolute_path(tmp_path):
    conn = PSConnector()
    app = FakeApp()
    conn.app = app
    rel = Path("relative") / "t.psd"

    result = conn.open_document(rel)

    opened = app.opened[0]
    assert Path(opened).is_absolute(), "相对路径会造成 Open 歧义，必须先 resolve"
    assert Path(opened).name == "t.psd"
    assert result == f"doc:{opened}"


def test_open_document_failure_wraps_with_guidance(tmp_path):
    conn = PSConnector()
    app = FakeApp()
    app.open_error = ValueError("user cancelled")
    conn.app = app

    with pytest.raises(RuntimeError) as ei:
        conn.open_document(tmp_path / "模板.psd")
    msg = str(ei.value)
    assert "PSD 文件无法打开" in msg
    assert "user cancelled" in msg
    assert "弹窗" in msg, "必须提示检查未处理的弹窗，否则员工只会反复点"


def test_set_display_dialogs_sets_mode():
    conn = PSConnector()
    app = FakeApp()
    conn.app = app
    conn.set_display_dialogs(1)
    assert app.DisplayDialogs == 1


def test_set_display_dialogs_without_app_is_noop():
    conn = PSConnector()
    conn.set_display_dialogs(3)  # 不允许抛


def test_set_display_dialogs_swallows_com_error():
    conn = PSConnector()

    class BadDialogApp:
        @property
        def DisplayDialogs(self):
            raise AssertionError("should not be read")

        @DisplayDialogs.setter
        def DisplayDialogs(self, value):
            raise _com_error(-2147352567, "denied")

    conn.app = BadDialogApp()
    conn.set_display_dialogs(3)  # COM 抑制策略：设置失败不阻断任务


def test_close_document_closes_without_saving():
    conn = PSConnector()
    closed: list[int] = []
    conn.close_document(SimpleNamespace(Close=lambda mode: closed.append(mode)))
    assert closed == [2], "2 = psDoNotSaveChanges，关文档绝不能弹保存框"


def test_disconnect_clears_app():
    conn = PSConnector()
    conn.app = FakeApp()
    conn.disconnect()
    assert conn.app is None
    conn.disconnect()  # 重复断开不抛


# ========== 纯文本逻辑 ==========

@pytest.mark.parametrize(
    "source, pattern, regex, match_mode, expected",
    [
        ("abc", "abc", False, "精确", True),
        ("abc", "ab", False, "精确", False),  # 精确 = 全等，部分命中不算
        ("a\rb", "a\nb", False, "精确", True),  # PS 的 \r 换行必须归一
        ("前缀 abc 后缀", "abc", False, "包含", True),
        ("前缀 abc 后缀", "xyz", False, "包含", False),
        ("稿号 123", r"\d+", True, "包含", True),
        ("稿号 abc", r"\d+", True, "包含", False),
    ],
)
def test_match_text(source, pattern, regex, match_mode, expected):
    conn = PSConnector()
    assert conn._match_text(source, pattern, regex, match_mode) is expected


def test_replace_text_modes():
    conn = PSConnector()
    assert conn._replace_text("原标题", "原标题", "新标题", False, "精确") == "新标题"
    assert conn._replace_text("a-x b-x", "x", "Y", False, "包含") == "a-Y b-Y"
    assert conn._replace_text("稿号 9", r"\d+", "8", True, "包含") == "稿号 8"


def test_replace_text_regex_keeps_backref_literal():
    """替换串里的 \\1 当字面量：被 re.sub 解析成反向引用会篡改编号甚至报错"""
    conn = PSConnector()
    result = conn._replace_text("稿号 A-1", r"(A)-\d", r"\1-009", True, "包含")
    assert result == r"稿号 \1-009"


# ========== 文档遍历 / 替换 ==========

class FakeTextItem:
    def __init__(self, contents: str):
        self.Contents = contents


class FakeLayer:
    def __init__(self, name="图层", kind=2, text: str | None = None):
        self.Name = name
        self.Kind = kind
        self.TypeName = "ArtLayer"
        if text is not None:
            self.TextItem = FakeTextItem(text)


class FakeLayerSet:
    def __init__(self, layers, name="组"):
        self.Name = name
        self.TypeName = "LayerSet"
        self.Layers = layers


class FakeDoc:
    def __init__(self, layers):
        self.Layers = list(layers)


def test_get_all_text_layers_nested_and_skips_junk():
    conn = PSConnector()
    inner = FakeLayer(name="内", text="内容 A")
    empty = FakeLayer(name="空", text="")
    broken = FakeLayer(name="坏", kind=2)  # Kind==2 但没有 TextItem
    group = FakeLayerSet([inner, empty, broken])
    doc = FakeDoc([FakeLayer(name="顶", text="内容 B"), group])

    assert conn.get_all_text_layers(doc) == ["内容 B", "内容 A"]
    assert conn.get_all_text_layers(None) == []


def test_get_all_text_layers_survives_com_error_on_textitem():
    conn = PSConnector()

    class PoisonLayer:
        Name = "毒图层"
        Kind = 2
        TypeName = "ArtLayer"

        @property
        def TextItem(self):
            raise _com_error(-2147467259)

    doc = FakeDoc([PoisonLayer(), FakeLayer(name="好", text="内容 X")])
    assert conn.get_all_text_layers(doc) == ["内容 X"], "坏图层不许拖垮整篇扫描"


def test_apply_replacements_returns_first_and_updates_layers():
    conn = PSConnector()
    deco = FakeLayer(name="装饰图层", kind=1)  # 非文本图层直接跳过
    first_layer = FakeLayer(name="正文", text="作者：{{作者}}")
    second_layer = FakeLayer(name="标题", text="题目：{{标题}}")
    doc = FakeDoc([deco, FakeLayerSet([first_layer]), second_layer])

    result = conn.apply_text_replacements(doc, [
        {"find": "{{作者}}", "replace": "李四", "regex": False, "match_mode": "包含"},
        {"find": "{{标题}}", "replace": "草莓种植", "regex": False, "match_mode": "包含"},
    ])

    assert result == "李四", "首个替换文本用于成品文件名"
    assert first_layer.TextItem.Contents == "作者：李四"
    assert second_layer.TextItem.Contents == "题目：草莓种植"
    assert conn.last_replacement_warnings == []


def test_apply_replacements_invalid_regex_warns_not_silent():
    conn = PSConnector()
    layer = FakeLayer(name="坏规则图层", text="稿号 12")
    doc = FakeDoc([layer])

    result = conn.apply_text_replacements(doc, [
        {"find": "[unclosed", "replace": "x", "regex": True, "match_mode": "包含"},
    ])

    assert result is None
    assert layer.TextItem.Contents == "稿号 12", "坏规则的图层内容必须原样保留"
    warns = conn.last_replacement_warnings
    assert any("正则规则无效" in w and "坏规则图层" in w for w in warns)


def test_apply_replacements_skips_empty_find():
    conn = PSConnector()
    layer = FakeLayer(name="正文", text="abc")
    conn.apply_text_replacements(FakeDoc([layer]), [{"find": "", "replace": "X"}])
    assert layer.TextItem.Contents == "abc"
    assert conn.last_replacement_warnings == []


def test_apply_replacements_survives_broken_layers():
    """坏图层（缺属性 / 意外异常）记录后继续，好图层必须照样替换"""
    conn = PSConnector()
    no_textitem = FakeLayer(name="伪文本", kind=2)
    doc = FakeDoc([no_textitem])

    class BoomItem:
        @property
        def Contents(self):
            raise ValueError("boom")

    boom = FakeLayer(name="炸属性", kind=2)
    boom.TextItem = BoomItem()
    good = FakeLayer(name="正文", text="作者：{{作者}}")
    doc.Layers = [no_textitem, boom, good]

    result = conn.apply_text_replacements(doc, [
        {"find": "{{作者}}", "replace": "李四", "regex": False, "match_mode": "包含"},
    ])
    assert result == "李四"
    assert good.TextItem.Contents == "作者：李四"


def test_apply_replacements_regex_mode():
    conn = PSConnector()
    layer = FakeLayer(name="编号", text="编号：001")
    result = conn.apply_text_replacements(FakeDoc([layer]), [
        {"find": r"编号：\d+", "replace": "编号：888", "regex": True, "match_mode": "包含"},
    ])
    assert result == "编号：888"
    assert layer.TextItem.Contents == "编号：888"


# ========== 导出 ==========

def _fake_doc():
    saved: list[tuple] = []

    class Doc:
        def SaveAs(self, path, options, as_copy=True):
            saved.append((path, options, as_copy))

    return Doc(), saved


def test_export_jpg_sets_quality_and_calls_saveas(tmp_path, monkeypatch):
    dispatched: list[str] = []

    def dispatch(progid):
        dispatched.append(progid)
        return SimpleNamespace()

    monkeypatch.setattr(m, "win32com", _fake_win32com(dispatch))
    conn = PSConnector()
    doc, saved = _fake_doc()

    conn.export_document(doc, tmp_path / "o.jpg", "JPG", quality=7)

    assert dispatched == ["Photoshop.JPEGSaveOptions"]
    assert saved[0][0] == str(tmp_path / "o.jpg")
    assert saved[0][1].Quality == 7
    assert saved[0][2] is True


def test_export_png_uses_png_options(tmp_path, monkeypatch):
    dispatched: list[str] = []
    monkeypatch.setattr(
        m, "win32com", _fake_win32com(lambda p: dispatched.append(p) or SimpleNamespace())
    )
    conn = PSConnector()
    doc, saved = _fake_doc()

    conn.export_document(doc, tmp_path / "o.png", "png")
    assert dispatched == ["Photoshop.PNGSaveOptions"]
    assert len(saved) == 1


def test_export_unsupported_format_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "win32com", _fake_win32com(lambda p: SimpleNamespace()))
    conn = PSConnector()
    doc, saved = _fake_doc()

    with pytest.raises(ValueError, match="Unsupported output format"):
        conn.export_document(doc, tmp_path / "o.gif", "GIF")
    assert saved == [], "不支持的格式不许走到 SaveAs"
