"""图片落盘与缩略图"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from pathlib import Path

from .config import IMAGES_DIR

logger = logging.getLogger(__name__)

ALLOWED_EXT = {"jpg", "jpeg", "png", "tif", "tiff", "psd"}
THUMB_MAX_WIDTH = 480


def _ext_of(filename: str, fallback: str = "jpg") -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return suffix if suffix in ALLOWED_EXT else fallback


def save_image(file_storage, work_date: date) -> tuple[str, str, str]:
    """
    保存上传的成品图，返回 (原图路径, 缩略图路径或空串, 原始文件名)。

    目录按 年/月/日 分层：单目录堆几万张图时，资源管理器打开都会卡。
    """
    original_name = (file_storage.filename or "image.jpg").strip()
    ext = _ext_of(original_name)
    day_dir = IMAGES_DIR / f"{work_date:%Y}" / f"{work_date:%m}" / f"{work_date:%d}"
    day_dir.mkdir(parents=True, exist_ok=True)

    stem = uuid.uuid4().hex
    image_path = day_dir / f"{stem}.{ext}"
    file_storage.save(image_path)

    thumb_path = ""
    if ext in ("jpg", "jpeg", "png"):
        try:
            from PIL import Image

            with Image.open(image_path) as img:
                img = img.convert("RGB")
                if img.width > THUMB_MAX_WIDTH:
                    ratio = THUMB_MAX_WIDTH / img.width
                    img = img.resize((THUMB_MAX_WIDTH, int(img.height * ratio)))
                thumb_file = day_dir / f"{stem}_thumb.jpg"
                img.save(thumb_file, "JPEG", quality=82, optimize=True)
                thumb_path = str(thumb_file)
        except Exception as e:  # noqa: BLE001 - 缩略图失败不影响主流程
            logger.warning("生成缩略图失败（回退用原图）: %s", e)

    return str(image_path), thumb_path, original_name


def delete_image(image_path: str | None, thumb_path: str | None) -> None:
    for path in (image_path, thumb_path):
        if not path:
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("删除图片失败 %s: %s", path, e)
