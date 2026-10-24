"""导出成品校验测试（core/export_check）

守的是"后台记成成功、交付时才发现图不对"这一类：尺寸错、全白、半截文件、CMYK 偏色。
这里全部用 Pillow 现造真文件——校验的对象是磁盘上的字节，不是任何返回值。
"""

from __future__ import annotations

from pathlib import Path

from core.export_check import (
    MIN_BYTES,
    inspect_image,
    sweep_stale_temps,
    temp_output_path,
)


def make_image(path: Path, size=(800, 600), color=(30, 60, 120), mode="RGB", marks=True) -> Path:
    """造一张正常成品：有线条和色块，灰度极差远大于"纯色"阈值"""
    from PIL import Image, ImageDraw

    base = color if mode == "RGB" else 128
    img = Image.new(mode, size, base if marks else (255, 255, 255) if mode == "RGB" else 255)
    if marks:
        line = (20, 40, 90) if mode == "RGB" else 10
        block = (200, 30, 60) if mode == "RGB" else 240
        draw = ImageDraw.Draw(img)
        for i in range(0, size[0], 40):
            draw.line([(i, 0), (i, size[1])], fill=line, width=3)
        draw.rectangle([20, 20, max(21, size[0] - 20), 60], fill=block)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG" if path.suffix.lower() in (".jpg", ".jpeg") else "PNG", quality=90)
    return path


# ========== 正常成品 ==========
def test_valid_image_passes(tmp_path):
    path = make_image(tmp_path / "ok.jpg")

    result = inspect_image(path)

    assert result.ok is True
    assert "800×600" in result.detail


def test_valid_image_with_matching_template_size_passes(tmp_path):
    path = make_image(tmp_path / "ok.jpg", size=(800, 600))

    assert inspect_image(path, expect_size=(800, 600)).ok is True


def test_transposed_template_size_is_accepted(tmp_path):
    """模板横竖与导出方向相反时不该拦——尺寸集合相同就是同一张图"""
    path = make_image(tmp_path / "ok.jpg", size=(600, 800))

    assert inspect_image(path, expect_size=(800, 600)).ok is True


def test_grayscale_image_is_not_treated_as_wrong_colorspace(tmp_path):
    """灰度模板是合法的，别把 L 当偏色拦下来"""
    path = make_image(tmp_path / "gray.jpg", mode="L", color=128)

    assert inspect_image(path).ok is True


# ========== 四类假成功 ==========
def test_size_mismatch_is_rejected_and_kept(tmp_path):
    """§1 尺寸错：模板 800×600 出 400×300（画布被裁/改过）"""
    path = make_image(tmp_path / "small.jpg", size=(400, 300))

    result = inspect_image(path, expect_size=(800, 600))

    assert result.ok is False
    assert "尺寸" in result.reason and "400×300" in result.reason and "800×600" in result.reason
    assert result.keep is True, "成型但尺寸不对的图要留档给员工核对"


def test_blank_image_is_rejected_and_kept(tmp_path):
    """§2 几乎纯色：文字层没渲染出来，白底一片"""
    path = make_image(tmp_path / "blank.jpg", marks=False)

    result = inspect_image(path)

    assert result.ok is False
    assert "纯色" in result.reason
    assert result.keep is True


def test_truncated_jpeg_is_rejected_and_deleted(tmp_path):
    """§3 半截文件：SaveAs 被打断，留下一个名字合规的残缺 JPG"""
    good = make_image(tmp_path / "good.jpg")
    raw = good.read_bytes()
    cut = tmp_path / "cut.jpg"
    cut.write_bytes(raw[: len(raw) // 3])

    result = inspect_image(cut)

    assert result.ok is False
    assert "不完整" in result.reason
    assert result.keep is False, "坏图必须删掉，不能留在输出目录等员工误交"


def test_garbage_bytes_are_rejected(tmp_path):
    path = tmp_path / "fake.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 4000)

    result = inspect_image(path)

    assert result.ok is False and result.keep is False


def test_tiny_file_is_rejected_before_decoding(tmp_path):
    """0 字节 / 几百字节：连解码都不用试"""
    path = tmp_path / "empty.jpg"
    path.write_bytes(b"x")

    result = inspect_image(path)

    assert result.ok is False
    assert str(MIN_BYTES) not in result.reason  # 不把内部阈值丢给员工看
    assert "不完整" in result.reason


def test_cmyk_image_is_rejected_and_kept(tmp_path):
    """§4 偏色：CMYK 直接导 JPG，绝大多数看图软件显示失真"""
    from PIL import Image

    path = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (800, 600), (0, 120, 200, 0)).save(path, "JPEG", quality=90)

    result = inspect_image(path)

    assert result.ok is False
    assert "CMYK" in result.reason and "偏色" in result.reason
    assert result.keep is True


def test_missing_file_is_reported_not_raised(tmp_path):
    result = inspect_image(tmp_path / "nope.jpg")

    assert result.ok is False
    assert "读不到" in result.reason


# ========== 临时名与清理 ==========
def test_temp_output_path_keeps_extension(tmp_path):
    """扩展名必须保留：PS 靠它（配合 SaveOptions）决定导出格式"""
    assert temp_output_path(tmp_path / "李四_标题.jpg").name == "李四_标题.psluyong-tmp.jpg"
    assert temp_output_path(tmp_path / "a.png").suffix == ".png"


def test_sweep_removes_old_temps_only(tmp_path):
    import os
    import time

    old_temp = tmp_path / "旧.psluyong-tmp.jpg"
    old_temp.write_bytes(b"x")
    stale = time.time() - 7200
    os.utime(old_temp, (stale, stale))
    fresh_temp = tmp_path / "新.psluyong-tmp.jpg"
    fresh_temp.write_bytes(b"x")
    employee_file = tmp_path / "员工的图.jpg"
    employee_file.write_bytes(b"x")

    removed = sweep_stale_temps(tmp_path)

    assert removed == 1
    assert not old_temp.exists()
    assert fresh_temp.exists(), "刚写的临时文件可能正在被用，不能删"
    assert employee_file.exists(), "员工自己的文件一律不动"


def test_sweep_on_missing_dir_is_noop(tmp_path):
    assert sweep_stale_temps(tmp_path / "不存在") == 0
