"""图片路径解析与上传校验：换机迁移后老绝对路径必须还能找到文件"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

import pytest
from werkzeug.datastructures import FileStorage

from server.app import storage


def test_relative_path_resolves_under_images_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    got = storage.resolve_image_path("2026/10/21/abc.jpg")
    assert got == tmp_path / "2026" / "10" / "21" / "abc.jpg"


def test_legacy_windows_absolute_path(tmp_path, monkeypatch):
    """从 Windows 迁到 NAS：库里存的是 E:\\...\\images\\2026\\... """
    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    stored = r"E:\psluyong-lite\server\data\images\2026\10\21\abc.jpg"
    got = storage.resolve_image_path(stored)
    assert got == tmp_path / "2026" / "10" / "21" / "abc.jpg"


def test_legacy_container_absolute_path(tmp_path, monkeypatch):
    """旧容器里存的是 /app/data/images/... """
    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    got = storage.resolve_image_path("/app/data/images/2026/10/21/abc.jpg")
    assert got == tmp_path / "2026" / "10" / "21" / "abc.jpg"


def test_none_and_empty():
    assert storage.resolve_image_path(None) is None
    assert storage.resolve_image_path("") is None


def test_tampered_absolute_path_rejected(tmp_path, monkeypatch):
    """库被篡改指向 images 目录之外的系统文件：必须拒绝。

    返回 None 后调用方按既有契约处理：/image 路由 404，delete_image 跳过删除——
    否则改库就能让接口吐出任意本地文件，或让删除功能删到目录外。
    """
    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    assert storage.resolve_image_path(r"C:\Windows\win.ini") is None
    assert storage.resolve_image_path("/etc/passwd") is None


def test_traversal_out_of_images_dir_rejected(tmp_path, monkeypatch):
    """images/ 段之后跟 .. 穿越，不允许逃出 IMAGES_DIR"""
    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    assert storage.resolve_image_path("images/../../../evil.jpg") is None
    assert storage.resolve_image_path("2026/../../evil.jpg") is None


def test_absolute_path_inside_images_dir_allowed(tmp_path, monkeypatch):
    """同机合法绝对路径（自定义 IMAGES_DIR 名、路径里没有 images 段）不能被误杀"""
    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    stored = str(tmp_path / "2026" / "10" / "abc.jpg")
    assert storage.resolve_image_path(stored) == Path(stored)


def test_save_image_stores_relative_path(tmp_path, monkeypatch):
    """新写入的数据必须是相对路径，否则换机又会坏"""
    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(storage, "THUMB_MAX_WIDTH", 480)

    from datetime import date
    from io import BytesIO

    from PIL import Image
    from werkzeug.datastructures import FileStorage

    buf = BytesIO()
    Image.new("RGB", (800, 600), (200, 30, 30)).save(buf, "JPEG")
    buf.seek(0)
    fs = FileStorage(stream=buf, filename="成品.jpg")

    image_path, thumb_path, original = storage.save_image(fs, date(2026, 10, 21))

    assert original == "成品.jpg"
    assert not Path(image_path).is_absolute()
    assert image_path.startswith("2026/10/21/")
    assert image_path.endswith(".jpg")
    assert (tmp_path / image_path).exists()

    assert thumb_path and not Path(thumb_path).is_absolute()
    assert thumb_path.endswith("_thumb.jpg")
    assert (tmp_path / thumb_path).exists()


def test_delete_image_uses_resolver(tmp_path, monkeypatch):
    """删除也要走解析，否则换机后删不掉旧图"""
    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    target = tmp_path / "2026" / "10" / "21" / "abc.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    storage.delete_image("/app/data/images/2026/10/21/abc.jpg", None)
    assert not target.exists()


def test_save_image_rejects_fake_image(tmp_path, monkeypatch):
    """改了扩展名的非图片必须被拒。

    原来只按扩展名放行，images/ 目录会慢慢混进打不开的"图片"：
    后台点开全是裂图，而备份还在每天忠实地镜像这些垃圾。
    """
    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    fake = FileStorage(stream=BytesIO(b"MZ\x90\x00 this is an exe, not a jpg"), filename="evil.jpg")

    with pytest.raises(storage.ImageValidationError):
        storage.save_image(fake, date.today())

    # 落盘后校验失败必须把文件删掉，不能留垃圾
    # （空的年/月/日 目录留着无妨，一天最多一个）
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_save_image_rejects_renamed_psd(tmp_path, monkeypatch):
    """psd 曾经在成品图白名单里，改成 .jpg 就能塞进图片目录"""
    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    psd = FileStorage(stream=BytesIO(b"8BPS" + b"\0" * 64), filename="template.jpg")

    with pytest.raises(storage.ImageValidationError):
        storage.save_image(psd, date.today())

    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_save_image_accepts_real_image(tmp_path, monkeypatch):
    """正常图片照旧：这次改动不能把真图挡在门外"""
    from PIL import Image

    monkeypatch.setattr(storage, "IMAGES_DIR", tmp_path)
    buf = BytesIO()
    Image.new("RGB", (900, 600), (10, 20, 30)).save(buf, "JPEG")
    buf.seek(0)

    image_path, thumb_path, name = storage.save_image(
        FileStorage(stream=buf, filename="成品.jpg"), date.today()
    )

    assert name == "成品.jpg"
    assert (tmp_path / image_path).exists()
    assert thumb_path, "JPEG 必须生成缩略图"
    assert (tmp_path / thumb_path).exists()
