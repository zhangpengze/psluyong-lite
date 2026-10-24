"""图片落盘与缩略图"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from pathlib import Path

from .config import IMAGES_DIR

logger = logging.getLogger(__name__)

ALLOWED_EXT = {"jpg", "jpeg", "png", "tif", "tiff"}
THUMB_MAX_WIDTH = 480


class ImageValidationError(Exception):
    """上传内容不是有效图片（扩展名对但内容是别的）"""


def _ext_of(filename: str, fallback: str = "jpg") -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return suffix if suffix in ALLOWED_EXT else fallback


def _to_stored(path: Path) -> str:
    """落库时存相对 IMAGES_DIR 的路径。

    存绝对路径会把部署位置写死进数据库：库从 Windows 迁到 NAS（或反过来）之后，
    所有图片都会因为路径对不上而 404。
    """
    try:
        return path.relative_to(IMAGES_DIR).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_image_path(stored: str | None, root: Path | None = None) -> Path | None:
    """把库里存的路径还原成当前机器上的实际路径。

    兼容三种历史写法：
      1. 相对路径（新数据）：`2026/10/21/xxx.jpg` → IMAGES_DIR 下
      2. 旧版绝对路径（同机）：`/app/data/images/...` 或 `E:\\...\\images\\...`
      3. 迁移过来的绝对路径（换机）：截取 `images` 之后的部分再拼当前 IMAGES_DIR

    注意不能用 Path.is_absolute() 判断：在 Windows 上 `/app/data/...` 不是绝对路径，
    但它是另一台 Linux 机器上的路径，必须按「含 images 段」来处理。

    root 只给测试与巡检脚本用（让它们能指向临时目录），生产调用一律走默认值，
    这样"应用看到的图片"和"巡检看到的图片"是同一套解析规则。
    """
    if not stored:
        return None
    base = root or IMAGES_DIR
    # Windows 的反斜杠在 Linux 上不是分隔符，统一成正斜杠再处理
    s = stored.replace("\\", "/").strip()

    if "images/" in s:
        tail = s.split("images/", 1)[1]
        return base.joinpath(*[p for p in tail.split("/") if p])

    is_rooted = s.startswith("/") or (len(s) > 1 and s[1] == ":")
    if is_rooted:
        return Path(s)
    return base / s


def save_image(file_storage, work_date: date) -> tuple[str, str, str]:
    """
    保存上传的成品图，返回 (原图路径, 缩略图路径或空串, 原始文件名)。

    目录按 年/月/日 分层：单目录堆几万张图时，资源管理器打开都会卡。
    返回的两个路径都是相对 IMAGES_DIR 的，换机器迁移不会失效。

    内容校验：扩展名只是个说法，谁都能把 exe 改名成 .jpg。落盘后用 Pillow
    真的解一次，解不开就删文件并抛 ImageValidationError —— 否则 images/
    目录会慢慢混进一堆打不开的"图片"，后台点开全是裂图，而备份还在忠实地
    把这些垃圾每天镜像一遍。
    """
    original_name = (file_storage.filename or "image.jpg").strip()
    ext = _ext_of(original_name)
    day_dir = IMAGES_DIR / f"{work_date:%Y}" / f"{work_date:%m}" / f"{work_date:%d}"
    day_dir.mkdir(parents=True, exist_ok=True)

    stem = uuid.uuid4().hex
    image_path = day_dir / f"{stem}.{ext}"
    file_storage.save(image_path)

    try:
        from PIL import Image

        with Image.open(image_path) as probe:
            probe.verify()  # 只读文件头与校验和，不解码整张图，快
    except ImportError:
        # 没装 Pillow 时退回"只信扩展名"，不能让上传整体挂掉
        logger.warning("未安装 Pillow，跳过图片内容校验")
    except Exception as e:  # noqa: BLE001 - 任何解码失败都算无效图片
        try:
            image_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("清理无效上传失败 %s", image_path)
        raise ImageValidationError("上传的文件不是有效图片") from e

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
                thumb_path = _to_stored(thumb_file)
        except Exception as e:  # noqa: BLE001 - 缩略图失败不影响主流程
            logger.warning("生成缩略图失败（回退用原图）: %s", e)

    return _to_stored(image_path), thumb_path, original_name


def delete_image(image_path: str | None, thumb_path: str | None) -> None:
    for stored in (image_path, thumb_path):
        path = resolve_image_path(stored)
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("删除图片失败 %s: %s", path, e)
