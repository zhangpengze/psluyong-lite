"""
一次生成的完整编排

调用方（客户端 sidecar）只需要构造 ExportRequest，拿到 ExportResult：
打开模板 → 替换两组文字 → 导生成片 → 关闭文档。
任何一步失败都返回结构化结果，不抛异常到上层。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List

from .ps_service import get_ps_service

logger = logging.getLogger(__name__)

# 模板里约定的两个占位符。用固定占位符而不是"图层名"，员工改模板时
# 只要占位符还在，生成就不会错位——这是这套方案能跑稳的关键。
DEFAULT_AUTHOR_PLACEHOLDER = "{{作者}}"
DEFAULT_TITLE_PLACEHOLDER = "{{标题}}"

# Windows 文件名非法字符
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


@dataclass
class ExportRequest:
    """一次生成请求"""

    template_path: Path
    author: str  # 替换1：作者
    title: str  # 替换2：标题
    output_dir: Path
    output_format: str = "JPG"
    jpg_quality: int = 10
    author_placeholder: str = DEFAULT_AUTHOR_PLACEHOLDER
    title_placeholder: str = DEFAULT_TITLE_PLACEHOLDER
    ps_path: str = ""


@dataclass
class ExportResult:
    """生成结果"""

    success: bool
    image_path: Path | None = None
    message: str = ""
    warnings: List[str] = field(default_factory=list)
    duration_ms: int = 0


def safe_filename(text: str, max_len: int = 40) -> str:
    """把用户输入变成可用的文件名片段"""
    cleaned = _ILLEGAL_CHARS.sub("_", (text or "").strip()).strip("._ ")
    return cleaned[:max_len] or "未命名"


def build_output_path(req: ExportRequest) -> Path:
    """输出路径：作者_标题_时间戳.jpg"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "jpg" if req.output_format.upper() in ("JPG", "JPEG") else req.output_format.lower()
    name = f"{safe_filename(req.author)}_{safe_filename(req.title)}_{stamp}.{suffix}"
    return Path(req.output_dir) / name


def run_export(req: ExportRequest) -> ExportResult:
    """执行一次生成。不抛异常，失败通过 ExportResult.message 返回。"""
    started = time.monotonic()

    def done(success: bool, **kwargs) -> ExportResult:
        kwargs.setdefault("duration_ms", int((time.monotonic() - started) * 1000))
        return ExportResult(success=success, **kwargs)

    template_path = Path(req.template_path)
    if not template_path.exists():
        return done(False, message=f"模板文件不存在: {template_path}")

    author = (req.author or "").strip()
    title = (req.title or "").strip()
    if not author or not title:
        return done(False, message="作者和标题都必须填写")

    output_dir = Path(req.output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return done(False, message=f"无法创建输出目录 {output_dir}: {e}")

    service = get_ps_service()

    # 已连接就不重复连——PS 冷启动要十几秒，每次重连体验会很差
    if not service.is_connected():
        ok, error = service.connect(req.ps_path)
        if not ok:
            return done(False, message=error)

    service.set_display_dialogs(3)  # 不弹任何对话框，否则批处理会卡在弹窗上

    ok, error = service.open_document(template_path)
    if not ok:
        return done(False, message=error)

    try:
        replacements = [
            {"find": req.author_placeholder, "replace": author, "regex": False, "match_mode": "精确"},
            {"find": req.title_placeholder, "replace": title, "regex": False, "match_mode": "精确"},
        ]
        ok, _, error = service.apply_text_replacements(replacements)
        warnings = list(getattr(service, "last_replacement_warnings", []) or [])
        if not ok:
            return done(False, message=error, warnings=warnings)

        output_path = build_output_path(req)
        ok, error = service.export_document(output_path, req.output_format, req.jpg_quality)
        if not ok:
            return done(False, message=error, warnings=warnings)

        return done(True, image_path=output_path, warnings=warnings)

    finally:
        # 无论成功失败都要关掉文档，否则下次打开会叠文档、PS 内存越用越大
        service.close_document(save_changes=False)
