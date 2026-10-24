"""本机文件操作（拆自 server.py）：模板名清洗/列表/定位、输出目录预检。

模板归员工本机管（不再从服务器下载），所以"哪些文件算模板、同名怎么处理、
路径能不能用"这些判断都收口在这里，路由层只做编排。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .api_client import ServerError
from .config import ClientConfig, template_dir_of

logger = logging.getLogger(__name__)

TEMPLATE_SUFFIX = ".psd"
# PSD 文件头魔数，用来挡掉改了扩展名的假文件（PS 打不开会在 COM 里报一堆错）
PSD_MAGIC = b"8BPS"
MAX_TEMPLATE_BYTES = 200 * 1024 * 1024
_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def sanitize_psd_name(raw: str) -> str:
    """保留中文的 PSD 文件名清洗。

    不能用 secure_filename：「中国收藏.psd」会被清成「psd」，
    模板名直接变成下拉框里的标签，必须保住中文。
    """
    stem = Path(raw or "").stem
    stem = _ILLEGAL_NAME_CHARS.sub("_", stem).strip().strip(". ")
    return f"{stem or '模板'}{TEMPLATE_SUFFIX}"


def _unique_target(base: Path, filename: str) -> Path:
    """同名模板不覆盖，自动加序号——员工重复上传不会悄悄顶掉旧模板"""
    target = base / filename
    if not target.exists():
        return target
    stem = Path(filename).stem
    for i in range(2, 1000):
        candidate = base / f"{stem}({i}){TEMPLATE_SUFFIX}"
        if not candidate.exists():
            return candidate
    raise OSError("同名模板太多，请先整理模板目录")


def list_local_templates(cfg: ClientConfig) -> list[dict]:
    """扫本机模板目录，列出可用 PSD。

    id 直接用文件名：同一目录内唯一，且能唯一定位到磁盘上的文件，
    不需要再下载或维护额外的 id 映射。
    """
    base = template_dir_of(cfg)
    if not base.is_dir():
        return []

    items: list[dict] = []
    for path in sorted(base.glob(f"*{TEMPLATE_SUFFIX}"), key=lambda p: p.name.lower()):
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError as e:  # 权限不足/文件被占用，跳过而不是整个列表报错
            logger.warning("模板文件读取失败 %s: %s", path.name, e)
            continue
        items.append(
            {
                "id": path.name,
                "name": path.stem,
                "filename": path.name,
                "size": size,
            }
        )
    return items


def _ensure_template(cfg: ClientConfig, template_id: str) -> tuple[Path, dict]:
    """把模板 id（文件名）解析成本机可用的 PSD 路径"""
    template = next((t for t in list_local_templates(cfg) if t["id"] == template_id), None)
    if template is None:
        raise ServerError("模板文件找不到了，请到设置里检查模板目录")
    return template_dir_of(cfg) / template["filename"], template


def _ensure_output_dir(cfg: ClientConfig) -> Path:
    """生成前预检输出目录：相对路径/不可写在这里就拦下来。

    不预检的代价：PS 白跑一轮 COM，最后报一句「文件不存在」，
    员工根本想不到是设置里的输出目录填错了（真实踩坑：填了「姓名」）。
    """
    raw = (cfg.output_dir or "").strip()
    if not raw:
        raise ServerError("输出目录未设置，请到设置里填写")
    target = Path(raw)
    if not target.is_absolute():
        raise ServerError(
            f"输出目录「{raw}」不是完整路径（应以盘符如 D:\\ 开头），请在设置里修正"
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ServerError(f"输出目录「{raw}」不可用：{e}，请在设置里修正") from None
    return target
