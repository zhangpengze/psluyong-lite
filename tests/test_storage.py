"""图片路径解析：换机迁移后老绝对路径必须还能找到文件"""

from __future__ import annotations

from pathlib import Path

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
