"""run_export 主链测试（PS 全程打桩，不需要真 Photoshop）

审计缺口：runner.py 68%，run_export 的主链（runner.py:120-169）一条没测——
真实链路上「模板不存在/参数空/目录建不出/连不上 PS/开文档失败/替换失败/
导出失败」都会走到，但没有任何用例证明它返回结构化失败而不是抛异常。

取消语义说明：runner 自身没有取消分支（取消在 client/sidecar/executor 层
杀 worker 进程），这里只验证 runner 的终态纪律：任何失败都关文档、不抛异常。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import runner as runner_mod
from core.runner import ExportRequest, run_export


class FakePSService:
    """可编排失败点的 PS 服务替身；记录每一次调用作为可观测证据

    export_document 会真的写一张图（Pillow 生成），否则成品校验无从谈起——
    校验的对象是"落盘的文件"，不是"COM 说成功了"。
    """

    def __init__(
        self,
        *,
        connected: bool = False,
        connect_ok: bool = True,
        open_ok: bool = True,
        replace_ok: bool = True,
        export_ok: bool = True,
        error: str = "PS 报错",
        image: str = "valid",
        size: tuple[int, int] = (800, 600),
        prep_warnings: list[str] | None = None,
    ):
        self._connected = connected
        self._connect_ok = connect_ok
        self._open_ok = open_ok
        self._replace_ok = replace_ok
        self._export_ok = export_ok
        self.error = error
        # image: valid / blank / truncated / garbage / mismatch
        self.image = image
        self.size = size
        self.prep_warnings = list(prep_warnings or [])
        self.calls: list[tuple] = []
        self.replacements: list[dict] = []
        self.exported: tuple | None = None
        self.closed_with: list[bool] = []
        self.last_replacement_warnings: list[str] = []

    def is_connected(self) -> bool:
        self.calls.append(("is_connected",))
        return self._connected

    def connect(self, ps_path: str = ""):
        self.calls.append(("connect", ps_path))
        return (True, "") if self._connect_ok else (False, self.error)

    def set_display_dialogs(self, mode: int = 3) -> None:
        self.calls.append(("set_display_dialogs", mode))

    def open_document(self, template_path: Path):
        self.calls.append(("open_document", Path(template_path)))
        return (True, "") if self._open_ok else (False, self.error)

    def document_info(self) -> dict:
        self.calls.append(("document_info",))
        return {"width": self.size[0], "height": self.size[1], "mode": 3, "bits": 8}

    def prepare_for_export(self):
        self.calls.append(("prepare_for_export",))
        return (True, list(self.prep_warnings))

    def apply_text_replacements(self, replacements):
        self.calls.append(("apply_text_replacements",))
        self.replacements = list(replacements)
        return (True, None, "") if self._replace_ok else (False, None, self.error)

    def export_document(self, output_path: Path, fmt: str, quality: int):
        self.calls.append(("export_document", Path(output_path), fmt, quality))
        self.exported = (Path(output_path), fmt, quality)
        if not self._export_ok:
            return (False, self.error)
        self._write_image(Path(output_path))
        return (True, "")

    def close_document(self, save_changes: bool = False) -> None:
        self.calls.append(("close_document", save_changes))
        self.closed_with.append(save_changes)

    # ---- 造图（模拟 PS 的真实产出）----
    def _write_image(self, path: Path) -> None:
        from PIL import Image, ImageDraw

        size = (400, 300) if self.image == "mismatch" else self.size
        if self.image == "garbage":
            path.write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 4000)
            return
        img = Image.new("RGB", size, (255, 255, 255))
        if self.image != "blank":
            # 画点东西，灰度极差远大于阈值——正常成品不是纯色
            draw = ImageDraw.Draw(img)
            for i in range(0, size[0], 40):
                draw.line([(i, 0), (i, size[1])], fill=(20, 40, 90), width=3)
            draw.rectangle([20, 20, size[0] - 20, 60], fill=(200, 30, 60))
        buf = path.with_suffix(".raw.jpg")
        img.save(buf, "JPEG", quality=90)
        if self.image == "truncated":
            raw = buf.read_bytes()
            buf.unlink()
            path.write_bytes(raw[: max(600, len(raw) // 3)])  # 半截文件
            return
        buf.replace(path)


def _install(monkeypatch, service: FakePSService) -> FakePSService:
    monkeypatch.setattr(runner_mod, "get_ps_service", lambda: service)
    return service


def _req(tmp_path: Path, **overrides) -> ExportRequest:
    kwargs = {
        "template_path": tmp_path / "模板.psd",
        "author": "李四",
        "title": "基于深度学习的图像分割方法研究",
        "output_dir": tmp_path / "out",
    }
    kwargs.update(overrides)
    if "template_path" not in overrides:
        kwargs["template_path"].write_bytes(b"8BPS" + b"\0" * 16)
    return ExportRequest(**kwargs)


def _names(calls: list[tuple]) -> list[str]:
    return [c[0] for c in calls]


# ---------- 成功主链 ----------

def test_run_export_happy_path_produces_image_path(tmp_path, monkeypatch):
    svc = _install(monkeypatch, FakePSService())

    result = run_export(_req(tmp_path))

    assert result.success is True
    assert result.image_path is not None
    assert result.image_path.name == "李四_基于深度学习的图像分割方法研究.jpg"
    assert result.image_path.parent == tmp_path / "out"
    assert (tmp_path / "out").is_dir()  # 输出目录必须被创建出来
    assert result.message == ""
    assert result.duration_ms >= 0
    # 终态纪律：文档一定被关闭（save_changes=False，绝不覆盖员工模板）
    assert svc.closed_with == [False]
    assert "set_display_dialogs" in _names(svc.calls)


def test_run_export_skips_reconnect_when_ps_already_connected(tmp_path, monkeypatch):
    """PS 已连接就不该再连一次——冷启动十几秒，重复连是真实体验问题"""
    svc = _install(monkeypatch, FakePSService(connected=True))

    result = run_export(_req(tmp_path))

    assert result.success is True
    assert "connect" not in _names(svc.calls), "已连接还去 connect 会白白等冷启动"


def test_run_export_connects_when_not_connected(tmp_path, monkeypatch):
    svc = _install(monkeypatch, FakePSService(connected=False))

    result = run_export(_req(tmp_path))

    assert result.success is True
    assert ("connect", "") in svc.calls


def test_run_export_uses_contains_match_for_placeholders(tmp_path, monkeypatch):
    """必须是「包含」匹配：真实模板里占位符嵌在整段话里，全等匹配命中不了"""
    svc = _install(monkeypatch, FakePSService())

    run_export(_req(tmp_path, author_placeholder="{{作者}}", title_placeholder="{{标题}}"))

    assert [r["find"] for r in svc.replacements] == ["{{作者}}", "{{标题}}"]
    assert [r["replace"] for r in svc.replacements] == ["李四", "基于深度学习的图像分割方法研究"]
    assert all(r["match_mode"] == "包含" for r in svc.replacements)
    assert all(r["regex"] is False for r in svc.replacements)


def test_run_export_passes_format_and_quality(tmp_path, monkeypatch):
    svc = _install(monkeypatch, FakePSService())

    run_export(_req(tmp_path, output_format="PNG", jpg_quality=8))

    assert svc.exported is not None
    _, fmt, quality = svc.exported
    assert (fmt, quality) == ("PNG", 8)
    assert svc.exported[0].suffix == ".png"


def test_run_export_carries_replacement_warnings_on_success(tmp_path, monkeypatch):
    """未命中警告要跟着成功结果一起给到员工，否则图错了没人知道"""
    svc = _install(monkeypatch, FakePSService())
    svc.last_replacement_warnings = ["第 2 条替换规则未命中，请核对成品"]

    result = run_export(_req(tmp_path))

    assert result.success is True
    assert result.warnings == ["第 2 条替换规则未命中，请核对成品"]


# ---------- 输入侧失败（PS 完全不被触碰） ----------

def test_run_export_missing_template_fails_before_touching_ps(tmp_path, monkeypatch):
    svc = _install(monkeypatch, FakePSService())

    result = run_export(_req(tmp_path, template_path=tmp_path / "不存在.psd"))

    assert result.success is False
    assert "模板文件不存在" in result.message
    assert svc.calls == [], "模板都没有就去连 PS 是浪费（连一次十几秒）"


@pytest.mark.parametrize(
    "author,title",
    [("", "标题"), ("   ", "标题"), ("作者", ""), ("作者", "  ")],
)
def test_run_export_blank_fields_fail_fast(tmp_path, monkeypatch, author, title):
    svc = _install(monkeypatch, FakePSService())

    result = run_export(_req(tmp_path, author=author, title=title))

    assert result.success is False
    assert result.message == "作者和标题都必须填写"
    assert svc.calls == []


def test_run_export_reports_unwritable_output_dir(tmp_path, monkeypatch):
    """输出目录建不出来必须是结构化失败，不是异常炸到 worker"""
    svc = _install(monkeypatch, FakePSService())
    blocked = tmp_path / "不是目录"
    blocked.write_bytes(b"x")

    result = run_export(_req(tmp_path, output_dir=blocked))

    assert result.success is False
    assert "无法创建输出目录" in result.message
    assert svc.calls == []


# ---------- PS 侧失败终态 ----------

def test_run_export_ps_connect_failure_is_terminal(tmp_path, monkeypatch):
    svc = _install(monkeypatch, FakePSService(connect_ok=False))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert result.message == "PS 报错"
    assert "open_document" not in _names(svc.calls)
    assert svc.closed_with == [], "文档没打开就不该去关（关一个不存在的文档会报错）"


def test_run_export_open_failure_is_terminal(tmp_path, monkeypatch):
    svc = _install(monkeypatch, FakePSService(open_ok=False))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert result.message == "PS 报错"
    assert "export_document" not in _names(svc.calls)
    assert svc.closed_with == []


def test_run_export_replacement_failure_returns_warnings_and_closes(tmp_path, monkeypatch):
    """零匹配治理判失败：警告要带上，文档也要关（否则 PS 里叠文档）"""
    svc = _install(monkeypatch, FakePSService(replace_ok=False))
    svc.last_replacement_warnings = ["占位符 {{标题}} 未命中"]

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert result.message == "PS 报错"
    assert result.warnings == ["占位符 {{标题}} 未命中"]
    assert svc.closed_with == [False]
    assert "export_document" not in _names(svc.calls)


def test_run_export_export_failure_is_terminal_but_closes_document(tmp_path, monkeypatch):
    """PS 导出失败是员工最常看到的失败：必须给失败终态 + 关文档 + 不抛异常"""
    svc = _install(monkeypatch, FakePSService(export_ok=False))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert result.message == "PS 报错"
    assert svc.exported is not None, "确实走到了导出这一步才失败"
    assert svc.closed_with == [False]  # finally 关文档：失败也不留叠文档
    assert not (tmp_path / "out" / "李四_基于深度学习的图像分割方法研究.jpg").exists()


def test_run_export_success_also_closes_document(tmp_path, monkeypatch):
    svc = _install(monkeypatch, FakePSService())

    run_export(_req(tmp_path))

    assert svc.closed_with == [False]


# ---------- 同名兜底 ----------

def test_run_export_timestamp_fallback_after_exhausted_numbers(tmp_path, monkeypatch):
    """(2)..(999) 全被占：退回带时间戳的名字，仍然不覆盖已有文件"""
    out = tmp_path / "out"
    out.mkdir()
    (out / "李四_标题.jpg").write_bytes(b"base")
    for i in range(2, 1000):
        (out / f"李四_标题({i}).jpg").write_bytes(b"x")

    svc = _install(monkeypatch, FakePSService())
    result = run_export(_req(tmp_path, author="李四", title="标题"))

    assert result.success is True
    assert re.fullmatch(r"李四_标题_\d{8}_\d{6}\.jpg", result.image_path.name)
    assert (out / "李四_标题.jpg").read_bytes() == b"base"  # 一张都不许覆盖
    assert svc.closed_with == [False]


# ---------- 成品校验：验过了才用交付名 ----------

def _out(tmp_path: Path) -> Path:
    return tmp_path / "out"


def test_run_export_writes_to_temp_name_then_publishes(tmp_path, monkeypatch):
    """导出先写 `*.psluyong-tmp.jpg`，校验通过才 rename 成交付名。

    这样"取消/崩溃留下一个名字合规的半截文件"在结构上不可能发生——
    员工按名字拿图，绝不会拿到半截的那张。
    """
    svc = _install(monkeypatch, FakePSService())

    result = run_export(_req(tmp_path))

    assert result.success is True
    exported_path = svc.exported[0]
    assert exported_path.name.endswith(".psluyong-tmp.jpg"), exported_path.name
    assert exported_path.name != result.image_path.name
    assert result.image_path.exists(), "交付名必须在校验通过后才出现"
    assert not exported_path.exists(), "临时文件不能留在输出目录里"
    assert list(_out(tmp_path).glob("*.psluyong-tmp*")) == []


def test_run_export_rejects_blank_image(tmp_path, monkeypatch):
    """全白图（文字层没渲染）不能算成功，也不能占用交付名"""
    svc = _install(monkeypatch, FakePSService(image="blank"))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert "纯色" in result.message
    assert not (_out(tmp_path) / "李四_基于深度学习的图像分割方法研究.jpg").exists()
    assert svc.closed_with == [False], "失败也要关文档"
    # 成型但可疑的图要留档给员工核对
    assert list(_out(tmp_path).glob("*校验未通过*.jpg"))


def test_run_export_rejects_truncated_image_and_deletes_it(tmp_path, monkeypatch):
    """半截 JPG 直接删掉：留着只会被当成成品交出去"""
    svc = _install(monkeypatch, FakePSService(image="truncated"))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert "不完整" in result.message
    assert list(_out(tmp_path).glob("*.jpg")) == [], "坏图一张都不许留"


def test_run_export_rejects_garbage_bytes(tmp_path, monkeypatch):
    svc = _install(monkeypatch, FakePSService(image="garbage"))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert "不完整" in result.message
    assert list(_out(tmp_path).glob("*.jpg")) == []


def test_run_export_rejects_size_mismatch_with_template(tmp_path, monkeypatch):
    """模板 800×600 却导出了 400×300：说明画布被改过，图不能用"""
    svc = _install(monkeypatch, FakePSService(image="mismatch", size=(800, 600)))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert "尺寸" in result.message and "800×600" in result.message
    assert not (_out(tmp_path) / "李四_基于深度学习的图像分割方法研究.jpg").exists()


def test_run_export_export_failure_leaves_no_temp_file(tmp_path, monkeypatch):
    """导出直接报错时，临时文件也不能留下（员工看到 tmp 会来问）"""
    svc = _install(monkeypatch, FakePSService(export_ok=False))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert list(_out(tmp_path).glob("*.psluyong-tmp*")) == []


def test_run_export_merges_preflight_warnings(tmp_path, monkeypatch):
    """CMYK 转 RGB 这类体检告警要跟替换告警一起给到员工"""
    svc = _install(monkeypatch, FakePSService(prep_warnings=["模板色彩模式不是 RGB（4），已转成 RGB 导出"]))
    svc.last_replacement_warnings = ["第 2 条替换规则未命中"]

    result = run_export(_req(tmp_path))

    assert result.success is True
    assert result.warnings == [
        "模板色彩模式不是 RGB（4），已转成 RGB 导出",
        "第 2 条替换规则未命中",
    ]


def test_run_export_sweeps_stale_temp_files(tmp_path, monkeypatch):
    """上次被取消/崩溃留下的临时文件，下次生成时顺手清掉"""
    import os
    import time

    out = _out(tmp_path)
    out.mkdir(parents=True)
    stale = out / "李四_标题.psluyong-tmp.jpg"
    stale.write_bytes(b"old")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    keep = out / "别人的图.jpg"
    keep.write_bytes(b"employee-file")

    _install(monkeypatch, FakePSService())
    run_export(_req(tmp_path))

    assert not stale.exists(), "两小时前的临时文件该清掉"
    assert keep.exists(), "员工自己的文件一律不许动"


# ---------- 成品校验：验过了才用交付名 ----------

def _out(tmp_path: Path) -> Path:
    return tmp_path / "out"


def test_run_export_writes_to_temp_name_then_publishes(tmp_path, monkeypatch):
    """导出先写 `*.psluyong-tmp.jpg`，校验通过才 rename 成交付名。

    这样"取消/崩溃留下一个名字合规的半截文件"在结构上不可能发生——
    员工按名字拿图，绝不会拿到半截的那张。
    """
    svc = _install(monkeypatch, FakePSService())

    result = run_export(_req(tmp_path))

    assert result.success is True
    exported_path = svc.exported[0]
    assert exported_path.name.endswith(".psluyong-tmp.jpg"), exported_path.name
    assert exported_path.name != result.image_path.name
    assert result.image_path.exists(), "交付名必须在校验通过后才出现"
    assert not exported_path.exists(), "临时文件不能留在输出目录里"
    assert list(_out(tmp_path).glob("*.psluyong-tmp*")) == []


def test_run_export_rejects_blank_image(tmp_path, monkeypatch):
    """全白图（文字层没渲染）不能算成功，也不能占用交付名"""
    svc = _install(monkeypatch, FakePSService(image="blank"))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert "纯色" in result.message
    assert not (_out(tmp_path) / "李四_基于深度学习的图像分割方法研究.jpg").exists()
    assert svc.closed_with == [False], "失败也要关文档"
    # 成型但可疑的图要留档给员工核对
    assert list(_out(tmp_path).glob("*校验未通过*.jpg"))


def test_run_export_rejects_truncated_image_and_deletes_it(tmp_path, monkeypatch):
    """半截 JPG 直接删掉：留着只会被当成成品交出去"""
    svc = _install(monkeypatch, FakePSService(image="truncated"))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert "不完整" in result.message
    assert list(_out(tmp_path).glob("*.jpg")) == [], "坏图一张都不许留"


def test_run_export_rejects_garbage_bytes(tmp_path, monkeypatch):
    svc = _install(monkeypatch, FakePSService(image="garbage"))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert "不完整" in result.message
    assert list(_out(tmp_path).glob("*.jpg")) == []


def test_run_export_rejects_size_mismatch_with_template(tmp_path, monkeypatch):
    """模板 800×600 却导出了 400×300：说明画布被改过，图不能用"""
    svc = _install(monkeypatch, FakePSService(image="mismatch", size=(800, 600)))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert "尺寸" in result.message and "800×600" in result.message
    assert not (_out(tmp_path) / "李四_基于深度学习的图像分割方法研究.jpg").exists()


def test_run_export_export_failure_leaves_no_temp_file(tmp_path, monkeypatch):
    """导出直接报错时，临时文件也不能留下（员工看到 tmp 会来问）"""
    svc = _install(monkeypatch, FakePSService(export_ok=False))

    result = run_export(_req(tmp_path))

    assert result.success is False
    assert list(_out(tmp_path).glob("*.psluyong-tmp*")) == []


def test_run_export_merges_preflight_warnings(tmp_path, monkeypatch):
    """CMYK 转 RGB 这类体检告警要跟替换告警一起给到员工"""
    svc = _install(monkeypatch, FakePSService(prep_warnings=["模板色彩模式不是 RGB（4），已转成 RGB 导出"]))
    svc.last_replacement_warnings = ["第 2 条替换规则未命中"]

    result = run_export(_req(tmp_path))

    assert result.success is True
    assert result.warnings == [
        "模板色彩模式不是 RGB（4），已转成 RGB 导出",
        "第 2 条替换规则未命中",
    ]


def test_run_export_sweeps_stale_temp_files(tmp_path, monkeypatch):
    """上次被取消/崩溃留下的临时文件，下次生成时顺手清掉"""
    import os
    import time

    out = _out(tmp_path)
    out.mkdir(parents=True)
    stale = out / "李四_标题.psluyong-tmp.jpg"
    stale.write_bytes(b"old")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    keep = out / "别人的图.jpg"
    keep.write_bytes(b"employee-file")

    _install(monkeypatch, FakePSService())
    run_export(_req(tmp_path))

    assert not stale.exists(), "两小时前的临时文件该清掉"
    assert keep.exists(), "员工自己的文件一律不许动"
