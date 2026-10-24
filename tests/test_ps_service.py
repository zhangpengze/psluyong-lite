"""core/ps_service 测试：服务封装主流程 + COM 错误人话翻译 + 零匹配/假成功治理

全部用假 connector / 假 PS 文档对象驱动，不依赖真 Photoshop：
  - 成功路径：连接、打开、替换、导出真实落盘、关文档
  - 错误路径：HRESULT 精确翻译、关键字规则（含顺序）、文件不存在、
    零匹配、导出假成功、COM 错误带目标路径
断言的是员工能看到的最终文案与返回状态——这些文案直接决定排障方向，
改坏任何一条（翻译表丢行、丢目标路径、零匹配放行）对应用例必须变红。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pywintypes
import pytest

import core.ps_service as ps_service_mod
from core.ps_service import PSService, get_ps_service


# ---------- 帮助对象 ----------

def _com_error(hresult: int, desc: str = "") -> pywintypes.com_error:
    """构造真实 com_error：(hresult, message, excepinfo, argerror)，
    excepinfo[1] 是描述文本，与生产里 PS 抛错的结构一致。"""
    return pywintypes.com_error(hresult, "system message", (None, desc, None, 0, None), None)


class FakeConnector:
    """替身 connector：脚本化 connect/open 的结果，记录调用序列"""

    def __init__(self) -> None:
        self.app = None
        self.connect_error: Exception | None = None
        self.open_error: Exception | None = None
        self.open_result = None
        self.connect_paths: list[str] = []
        self.closed: list = []
        self.dialog_modes: list[int] = []
        self.close_error: Exception | None = None

    def connect(self, ps_path: str) -> None:
        self.connect_paths.append(ps_path)
        if self.connect_error is not None:
            raise self.connect_error
        self.app = object()

    def open_document(self, path):
        if self.open_error is not None:
            raise self.open_error
        return self.open_result

    def close_document(self, doc) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed.append(doc)

    def set_display_dialogs(self, mode: int) -> None:
        self.dialog_modes.append(mode)


class FakeTextItem:
    def __init__(self, contents: str, font: str = "SourceHanSansSC-Regular", kind: int = 1):
        self.Contents = contents
        self.Font = font
        self.Kind = kind


class FakeLayer:
    def __init__(self, name="图层", kind=2, text: str | None = None, bounds=(0, 0, 100, 100)):
        self.Name = name
        self.Kind = kind
        self.TypeName = "ArtLayer"
        self.Bounds = list(bounds)
        if text is not None:
            self.TextItem = FakeTextItem(text)


class FakeLayerSet:
    def __init__(self, layers, name="组"):
        self.Name = name
        self.TypeName = "LayerSet"
        self.Layers = layers


class FakeDoc:
    """假 PS 文档：SaveAs 行为可脚本化（写盘 / 不写 / 抛错）"""

    def __init__(self, layers=(), width=800, height=600, fonts=("SourceHanSansSC-Regular",)):
        self.Layers = list(layers)
        self.Width = width
        self.Height = height
        self.Application = SimpleNamespace(Fonts=[SimpleNamespace(PostScriptName=f) for f in fonts])
        self.save_behavior = "write"  # write / noop / empty / 或异常实例
        self.saved: list[tuple] = []
        self.close_modes: list[int] = []

    def SaveAs(self, path, options, as_copy=True):
        self.saved.append((path, options, as_copy))
        if self.save_behavior == "write":
            Path(path).write_bytes(b"IMG")
        elif self.save_behavior == "noop":
            pass
        elif self.save_behavior == "empty":
            Path(path).write_bytes(b"")
        else:
            raise self.save_behavior

    def Close(self, mode: int) -> None:
        self.close_modes.append(mode)


def new_service():
    svc = PSService()  # 单例（autouse fixture 已重置）
    fake = FakeConnector()
    svc._connector = fake
    svc._current_document = None
    svc.last_replacement_warnings = []
    return svc, fake


@pytest.fixture(autouse=True)
def _reset_singleton():
    ps_service_mod.PSService._instance = None
    ps_service_mod._ps_service = None
    yield
    ps_service_mod.PSService._instance = None
    ps_service_mod._ps_service = None


# ========== 单例 ==========

def test_get_ps_service_is_singleton():
    a = get_ps_service()
    b = get_ps_service()
    assert a is b
    assert PSService() is a


# ========== connect ==========

def test_connect_success():
    svc, fake = new_service()
    assert svc.connect("C:/PS/Photoshop.exe") == (True, "")
    assert fake.connect_paths == ["C:/PS/Photoshop.exe"]
    assert svc.is_connected() is True


def test_connect_file_not_found_message():
    svc, fake = new_service()
    fake.connect_error = FileNotFoundError("C:/no/Photoshop.exe")
    ok, msg = svc.connect("C:/no/Photoshop.exe")
    assert ok is False
    assert msg.startswith("Photoshop 路径不存在:")


def test_connect_com_error_hresult_exact_translation():
    """HRESULT 精确命中：员工看到的必须是表里那句人话，不是原始 COM 代码"""
    svc, fake = new_service()
    fake.connect_error = _com_error(-2147221005)
    ok, msg = svc.connect("")
    assert ok is False
    assert msg == "Photoshop 未运行，请先启动 Photoshop 再点执行"


def test_connect_com_error_keyword_translation():
    """hresult 不在表里时走关键字规则：excepinfo 描述里的场景词要能命中"""
    svc, fake = new_service()
    fake.connect_error = _com_error(-2147352567, "The scratch disk is full")
    ok, msg = svc.connect("")
    assert ok is False
    assert "暂存盘" in msg


def test_connect_generic_error_wrapped():
    svc, fake = new_service()
    fake.connect_error = RuntimeError("boom")
    ok, msg = svc.connect("")
    assert ok is False
    assert msg == "无法连接到 Photoshop: boom"


# ========== COM 错误翻译规则 ==========

def test_hresult_table_other_entries_translated():
    svc, _ = new_service()
    assert svc._format_com_error(_com_error(-2147467259)) == (
        "Photoshop 操作失败或超时，请重试；若反复出现请重启 Photoshop"
    )
    assert svc._format_com_error(_com_error(-2147024784)) == "磁盘空间已满，请清理磁盘空间后重试"


def test_keyword_rule_具体场景先于通用场景():
    """scratch disk 必须命中暂存盘专项，而不是落到通用磁盘规则；
    license 必须先于 not found / disk 这类通用词。规则顺序错 → 这两条红。"""
    svc, _ = new_service()
    scratch = svc._format_com_error(_com_error(-2147352567, "scratch disk full"))
    assert "暂存盘" in scratch and "磁盘空间不足" not in scratch

    license_msg = svc._format_com_error(
        _com_error(-2147352567, "trial license expired, please activate")
    )
    assert "激活" in license_msg

    font_msg = svc._format_com_error(
        _com_error(-2147352567, "The font 'X' could not be found")
    )
    assert "字体" in font_msg and "文件不存在" not in font_msg


def test_format_com_error_unknown_falls_back_with_code():
    svc, _ = new_service()
    msg = svc._format_com_error(_com_error(-2147352567, "something nobody saw before"))
    assert msg.startswith("COM 错误 (代码: -2147352567)")
    assert "something nobody saw before" in msg


# ========== open / close / disconnect ==========

def test_open_document_success_and_closes_previous():
    svc, fake = new_service()
    prev, new = object(), object()
    svc._current_document = prev
    fake.open_result = new
    ok, msg = svc.open_document(Path("t.psd"))
    assert ok is True and msg == ""
    assert fake.closed == [prev], "打开新文档前必须先关旧的，否则 PS 里叠文档"
    assert svc._current_document is new


def test_open_document_file_not_found():
    svc, fake = new_service()
    fake.open_error = FileNotFoundError("t.psd")
    ok, msg = svc.open_document(Path("t.psd"))
    assert ok is False and msg.startswith("文档不存在:")


def test_open_document_com_error_translated():
    svc, fake = new_service()
    fake.open_error = _com_error(-2147467259)
    ok, msg = svc.open_document(Path("t.psd"))
    assert ok is False
    assert msg == "打开文档失败: Photoshop 操作失败或超时，请重试；若反复出现请重启 Photoshop"


def test_open_document_generic_error_wrapped():
    svc, fake = new_service()
    fake.open_error = RuntimeError("boom")
    ok, msg = svc.open_document(Path("t.psd"))
    assert ok is False and msg == "打开文档失败: boom"


def test_close_document_closes_with_no_save_and_clears():
    svc, _ = new_service()
    doc = FakeDoc()
    svc._current_document = doc
    svc.close_document(save_changes=False)
    assert doc.close_modes == [2], "默认必须用不保存方式关闭（2 = psDoNotSaveChanges）"
    assert svc._current_document is None


def test_close_document_swallows_close_error():
    svc, fake = new_service()
    svc._current_document = object()
    fake.close_error = RuntimeError("COM 已崩")
    svc.close_document()  # 不允许抛出，清理链路必须安全
    assert svc._current_document is None


def test_close_document_without_doc_is_noop():
    svc, fake = new_service()
    svc.close_document()
    assert fake.closed == []


def test_disconnect_clears_state_even_if_close_fails():
    svc, fake = new_service()
    svc._current_document = object()
    fake.close_error = RuntimeError("gone")
    fake.app = object()
    svc.disconnect()
    assert svc._current_document is None
    assert svc.is_connected() is False


def test_is_connected_reflects_connector_app():
    svc, fake = new_service()
    assert svc.is_connected() is False
    fake.app = object()
    assert svc.is_connected() is True


def test_set_display_dialogs_delegates_to_connector():
    svc, fake = new_service()
    svc.set_display_dialogs(2)
    svc.set_display_dialogs()  # 默认 3 = 不显示任何对话框
    assert fake.dialog_modes == [2, 3]


# ========== 文本替换：成功路径 ==========

def test_apply_replacements_contains_match_success():
    svc, _ = new_service()
    layer = FakeLayer(name="正文", text="作者：{{作者}}")
    doc = FakeDoc([layer])
    svc._current_document = doc

    ok, first, err = svc.apply_text_replacements(
        [{"find": "{{作者}}", "replace": "李四", "regex": False, "match_mode": "包含"}]
    )
    assert (ok, first, err) == (True, "李四", "")
    assert layer.TextItem.Contents == "作者：李四"
    assert layer.TextItem.Contents != "作者：{{作者}}", "占位符必须被真替换掉"


def test_apply_replacements_exact_mode():
    svc, _ = new_service()
    layer = FakeLayer(name="标题", text="原标题")
    svc._current_document = FakeDoc([layer])

    ok, first, err = svc.apply_text_replacements(
        [{"find": "原标题", "replace": "新标题", "regex": False, "match_mode": "精确"}]
    )
    assert ok is True and first == "新标题"
    assert layer.TextItem.Contents == "新标题"


def test_apply_replacements_exact_mode_requires_full_equality():
    svc, _ = new_service()
    layer = FakeLayer(name="标题", text="包含原标题的长句")
    svc._current_document = FakeDoc([layer])

    ok, _, err = svc.apply_text_replacements(
        [{"find": "原标题", "replace": "新标题", "regex": False, "match_mode": "精确"}]
    )
    assert ok is False, "精确模式部分命中不算成功，否则会导出没改过的图"


def test_apply_replacements_regex_mode():
    svc, _ = new_service()
    layer = FakeLayer(name="编号", text="编号：001")
    svc._current_document = FakeDoc([layer])

    ok, first, _ = svc.apply_text_replacements(
        [{"find": r"编号：\d+", "replace": "编号：888", "regex": True, "match_mode": "包含"}]
    )
    assert ok is True and first == "编号：888"
    assert layer.TextItem.Contents == "编号：888"


def test_apply_replacements_regex_treats_backref_as_literal():
    """替换文本里的 \\1 必须当字面量：模板公司常用 \\1 这种编号写法，
    被 re.sub 当反向引用会改出错图甚至直接报错。"""
    svc, _new = new_service()
    layer = FakeLayer(name="编号", text="稿号 A-1")
    svc._current_document = FakeDoc([layer])

    ok, _, _ = svc.apply_text_replacements(
        [{"find": r"(A)-\d", "replace": r"\1-009", "regex": True, "match_mode": "包含"}]
    )
    assert ok is True
    assert layer.TextItem.Contents == r"稿号 \1-009", "替换文本必须原样落盘，不解析反向引用"


def test_apply_replacements_recurses_into_layer_set():
    svc, _ = new_service()
    inner = FakeLayer(name="嵌套正文", text="题目：{{标题}}")
    group = FakeLayerSet([FakeLayer(name="非文本", kind=1), inner], name="内容组")
    svc._current_document = FakeDoc([group])

    ok, first, _ = svc.apply_text_replacements(
        [{"find": "{{标题}}", "replace": "草莓种植", "regex": False, "match_mode": "包含"}]
    )
    assert ok is True and first == "草莓种植"
    assert inner.TextItem.Contents == "题目：草莓种植"


def test_apply_replacements_no_document():
    svc, _ = new_service()
    svc._current_document = None
    ok, first, err = svc.apply_text_replacements([{"find": "a", "replace": "b"}])
    assert (ok, first) == (False, None)
    assert err == "没有打开的文档"


# ========== 零匹配与警告治理 ==========

def test_zero_match_returns_failure_not_fake_success():
    """一条都没替换成功时绝不能返回成功——否则导出的图和模板一模一样，
    员工以为做完了。"""
    svc, _ = new_service()
    svc._current_document = FakeDoc([FakeLayer(name="正文", text="没有占位符的正文")])

    ok, first, err = svc.apply_text_replacements(
        [{"find": "{{作者}}", "replace": "李四", "regex": False, "match_mode": "包含"}]
    )
    assert ok is False and first is None
    assert err == "未找到任何可替换的文本，请检查模板占位符与查找规则是否一致"


def test_zero_match_when_document_has_no_text_layers():
    svc, _ = new_service()
    svc._current_document = FakeDoc([FakeLayer(name="纯图层", kind=1)])  # Kind != 2
    ok, _, err = svc.apply_text_replacements([{"find": "a", "replace": "b"}])
    assert ok is False and "未找到任何可替换的文本" in err


def test_empty_find_rule_skipped_without_unmatched_warning():
    """空 find 是脏配置：跳过即可，不能误报「第 1 条未命中」吓员工"""
    svc, _ = new_service()
    svc._current_document = FakeDoc([FakeLayer(name="正文", text="普通正文")])

    ok, _, err = svc.apply_text_replacements([{"find": "", "replace": "X"}])
    assert ok is False and "未找到任何可替换的文本" in err
    assert svc.last_replacement_warnings == [], "空规则不产生未命中警告"


def test_unmatched_rule_produces_warning():
    svc, _ = new_service()
    layer = FakeLayer(name="正文", text="作者：{{作者}}")
    svc._current_document = FakeDoc([layer])

    ok, _, _ = svc.apply_text_replacements(
        [
            {"find": "{{作者}}", "replace": "李四", "regex": False, "match_mode": "包含"},
            {"find": "{{不存在的占位符}}", "replace": "王五", "regex": False, "match_mode": "包含"},
        ]
    )
    assert ok is True
    assert "第 2 条替换规则未命中，请核对成品" in svc.last_replacement_warnings


def test_invalid_regex_warning_keeps_processing():
    """坏正则只警告该图层，不能让整批生成崩掉，也不能静默吞掉"""
    svc, _ = new_service()
    bad = FakeLayer(name="坏规则图层", text="稿号 12")
    good = FakeLayer(name="正文", text="作者：{{作者}}")
    svc._current_document = FakeDoc([bad, good])

    ok, first, err = svc.apply_text_replacements(
        [
            {"find": "[unclosed", "replace": "x", "regex": True, "match_mode": "包含"},
            {"find": "{{作者}}", "replace": "李四", "regex": False, "match_mode": "包含"},
        ]
    )
    assert ok is True and first == "李四"
    warn = [w for w in svc.last_replacement_warnings if "正则规则无效" in w]
    assert warn and "坏规则图层" in warn[0]


def test_invalid_regex_on_layer_with_unknown_name():
    """图层名属性本身读不出时警告也不能炸——落到「未知图层」"""
    svc, _ = new_service()

    class NamelessLayer:
        Kind = 2
        TypeName = "ArtLayer"
        Bounds = [0, 0, 100, 100]

        def __init__(self, text: str):
            self.TextItem = FakeTextItem(text)

        @property
        def Name(self):
            raise ValueError("name gone")

    layer = NamelessLayer(text="稿号 12")
    svc._current_document = FakeDoc([layer])
    ok, _, _ = svc.apply_text_replacements(
        [{"find": "[unclosed", "replace": "x", "regex": True, "match_mode": "包含"}]
    )
    assert ok is False  # 零匹配
    assert any("未知图层" in w and "正则规则无效" in w for w in svc.last_replacement_warnings)


# ========== 替换后的告警（字体/画布） ==========

def test_font_list_unavailable_downgrades_to_warning():
    svc, _ = new_service()

    class NoFontsDoc:
        def __init__(self, layers):
            self.Layers = list(layers)
            self.Width = 800
            self.Height = 600

        @property
        def Application(self):
            raise AttributeError("fonts unavailable")

    layer = FakeLayer(name="正文", text="作者：{{作者}}")
    doc = NoFontsDoc([layer])
    svc._current_document = doc

    ok, _, _ = svc.apply_text_replacements(
        [{"find": "{{作者}}", "replace": "李四", "regex": False, "match_mode": "包含"}]
    )
    assert ok is True, "取不到字体表只降级为告警，不能阻断生成"
    assert "字体可用性未验证，请打开成品图确认" in svc.last_replacement_warnings


def test_font_missing_and_overflow_warnings():
    svc, _ = new_service()
    missing_font = FakeLayer(name="标题", text="作者：{{作者}}")
    missing_font.TextItem.Font = "NotInstalledFont"
    overflow = FakeLayer(name="长文", text="题目：{{题目}}", bounds=(0, 0, 9999, 9999))
    para = FakeLayer(name="段落", text="作者：{{作者}}")
    para.TextItem.Kind = 2  # 段落文本
    svc._current_document = FakeDoc([missing_font, overflow, para])

    ok, _, _ = svc.apply_text_replacements(
        [
            {"find": "{{作者}}", "replace": "李四", "regex": False, "match_mode": "包含"},
            {"find": "{{题目}}", "replace": "草莓种植", "regex": False, "match_mode": "包含"},
        ]
    )
    assert ok is True
    warns = svc.last_replacement_warnings
    assert any("图层「标题」字体不可用" in w for w in warns)
    assert any("图层「长文」文本超出画布" in w for w in warns)
    assert any("图层「段落」段落框内部溢出未验证" in w for w in warns)


def test_font_property_error_and_bounds_error_warn_not_raise():
    svc, _ = new_service()

    class BadFontItem:
        def __init__(self, contents: str):
            self.Contents = contents
            self.Kind = 1

        @property
        def Font(self):
            raise ValueError("font table gone")

    class BadBoundsLayer:
        Name = "怪图层"
        Kind = 2
        TypeName = "ArtLayer"

        def __init__(self):
            self.TextItem = BadFontItem("作者：{{作者}}")

        @property
        def Bounds(self):
            raise ValueError("bounds gone")

    svc._current_document = FakeDoc([BadBoundsLayer()])

    ok, _, _ = svc.apply_text_replacements(
        [{"find": "{{作者}}", "replace": "李四", "regex": False, "match_mode": "包含"}]
    )
    assert ok is True
    warns = svc.last_replacement_warnings
    assert any("字体未验证" in w for w in warns), "字体属性炸了要有告警"
    assert any("文本边界未验证" in w for w in warns), "边界读不出要有告警"


def test_layer_without_text_item_is_skipped_not_crash():
    svc, _ = new_service()
    broken = FakeLayer(name="伪文本", kind=2)  # Kind==2 但没有 TextItem 属性
    good = FakeLayer(name="正文", text="作者：{{作者}}")
    svc._current_document = FakeDoc([broken, good])

    ok, first, _ = svc.apply_text_replacements(
        [{"find": "{{作者}}", "replace": "李四", "regex": False, "match_mode": "包含"}]
    )
    assert ok is True and first == "李四", "坏图层跳过后好图层必须继续处理"


def test_apply_runtime_error_returned_as_failure():
    svc, _ = new_service()

    class BrokenDoc:
        @property
        def Layers(self):
            raise RuntimeError("PS 对象挂了")

    svc._current_document = BrokenDoc()
    ok, first, err = svc.apply_text_replacements([{"find": "a", "replace": "b"}])
    assert (ok, first) == (False, None)
    assert err == "文本替换失败: PS 对象挂了"


def test_apply_com_error_translated_in_failure_message():
    svc, _ = new_service()

    class BrokenDoc:
        @property
        def Layers(self):
            raise _com_error(-2147467259)

    svc._current_document = BrokenDoc()
    ok, _, err = svc.apply_text_replacements([{"find": "a", "replace": "b"}])
    assert ok is False
    assert "文本替换失败: Photoshop 操作失败或超时，请重试；若反复出现请重启 Photoshop" == err


def test_apply_attribute_error_returned_as_failure():
    svc, _ = new_service()

    class BrokenDoc:
        @property
        def Layers(self):
            raise AttributeError("no layers")

    svc._current_document = BrokenDoc()
    ok, _, err = svc.apply_text_replacements([{"find": "a", "replace": "b"}])
    assert ok is False and err == "文本替换失败: no layers"


def test_apply_unknown_exception_returned_as_failure():
    """既不是 COM 错误也不是 AttributeError/RuntimeError 的意外异常：
    必须兜底成失败返回，不能裸抛到 runner 之上"""
    svc, _ = new_service()

    class BrokenDoc:
        @property
        def Layers(self):
            raise ValueError("奇怪的错误")

    svc._current_document = BrokenDoc()
    ok, first, err = svc.apply_text_replacements([{"find": "a", "replace": "b"}])
    assert (ok, first) == (False, None)
    assert err == "文本替换失败: 奇怪的错误"


# ========== 导出：真实落盘校验 ==========

def _patch_dispatch(monkeypatch, recorded: list):
    def dispatch(progid):
        recorded.append(progid)
        return SimpleNamespace()

    monkeypatch.setattr("win32com.client.Dispatch", dispatch)


def test_export_jpg_success_writes_real_file(tmp_path, monkeypatch):
    svc, _ = new_service()
    out = tmp_path / "out.jpg"
    doc = FakeDoc()
    svc._current_document = doc
    recorded: list = []
    _patch_dispatch(monkeypatch, recorded)

    ok, msg = svc.export_document(out, "JPG", quality=8)
    assert (ok, msg) == (True, "")
    assert recorded == ["Photoshop.JPEGSaveOptions"]
    assert doc.saved[0][0] == str(out) and doc.saved[0][2] is True
    assert doc.saved[0][1].Quality == 8, "画质参数必须传给 SaveOptions"
    assert out.read_bytes() == b"IMG", "导出后文件必须真实落盘"


@pytest.mark.parametrize(
    "fmt, progid",
    [
        ("JPEG", "Photoshop.JPEGSaveOptions"),
        ("PNG", "Photoshop.PNGSaveOptions"),
        ("PSD", "Photoshop.PhotoshopSaveOptions"),
        ("TIF", "Photoshop.TiffSaveOptions"),
    ],
)
def test_export_format_dispatch_mapping(tmp_path, monkeypatch, fmt, progid):
    svc, _ = new_service()
    svc._current_document = FakeDoc()
    recorded: list = []
    _patch_dispatch(monkeypatch, recorded)

    ok, msg = svc.export_document(tmp_path / f"o.{fmt.lower()}", fmt)
    assert (ok, msg) == (True, "")
    assert recorded == [progid]


def test_export_no_document():
    svc, _ = new_service()
    svc._current_document = None
    ok, msg = svc.export_document(Path("x.jpg"))
    assert ok is False and msg == "没有打开的文档"


def test_export_unsupported_format():
    svc, _ = new_service()
    svc._current_document = FakeDoc()
    ok, msg = svc.export_document(Path("x.gif"), "GIF")
    assert ok is False and msg == "不支持的输出格式: GIF"


def test_export_fake_success_detected_when_file_not_written(tmp_path, monkeypatch):
    """假成功防线：COM SaveAs 返回了但文件没落盘 → 必须判失败"""
    svc, _ = new_service()
    doc = FakeDoc()
    doc.save_behavior = "noop"
    svc._current_document = doc
    _patch_dispatch(monkeypatch, [])

    out = tmp_path / "ghost.jpg"
    ok, msg = svc.export_document(out, "JPG")
    assert ok is False
    assert "导出文件未生成" in msg and str(out) in msg


def test_export_empty_file_treated_as_failure(tmp_path, monkeypatch):
    svc, _ = new_service()
    doc = FakeDoc()
    doc.save_behavior = "empty"
    svc._current_document = doc
    _patch_dispatch(monkeypatch, [])

    ok, msg = svc.export_document(tmp_path / "zero.jpg", "JPG")
    assert ok is False and "导出文件未生成" in msg


def test_export_file_not_found_path_message(tmp_path, monkeypatch):
    svc, _ = new_service()
    doc = FakeDoc()
    doc.save_behavior = FileNotFoundError("输出目录没了")
    svc._current_document = doc
    _patch_dispatch(monkeypatch, [])

    ok, msg = svc.export_document(tmp_path / "o.jpg", "JPG")
    assert ok is False and msg == "输出路径不存在: 输出目录没了"


def test_export_com_error_message_includes_target_path(tmp_path, monkeypatch):
    """通用翻译（如「文件不存在」）不说是哪个文件，员工永远想不到是
    输出目录填错了——所以导出失败文案必须带目标路径。"""
    svc, _ = new_service()
    doc = FakeDoc()
    doc.save_behavior = _com_error(-2147024894)
    svc._current_document = doc
    _patch_dispatch(monkeypatch, [])

    out = tmp_path / "李四_标题.jpg"
    ok, msg = svc.export_document(out, "JPG")
    assert ok is False
    assert "文件不存在，请检查文件路径是否正确" in msg
    assert f"（目标: {out}）" in msg


def test_export_generic_error_includes_target_path(tmp_path, monkeypatch):
    svc, _ = new_service()
    doc = FakeDoc()
    doc.save_behavior = RuntimeError("disk on fire")
    svc._current_document = doc
    _patch_dispatch(monkeypatch, [])

    out = tmp_path / "o.jpg"
    ok, msg = svc.export_document(out, "JPG")
    assert ok is False and msg == f"导出失败: disk on fire（目标: {out}）"


# ========== 文本图层体检 ==========

def test_get_all_text_layers_walks_nested_groups():
    svc, _ = new_service()
    inner = FakeLayer(name="内层", text="内容 A")
    empty = FakeLayer(name="空文本", text="")
    broken = FakeLayer(name="坏图层", kind=2)  # 无 TextItem
    group = FakeLayerSet([inner, empty, broken])
    svc._current_document = FakeDoc([FakeLayer(name="顶层", text="内容 B"), group])

    assert svc.get_all_text_layers() == ["内容 B", "内容 A"]


def test_get_all_text_layers_without_document():
    svc, _ = new_service()
    svc._current_document = None
    assert svc.get_all_text_layers() == []
