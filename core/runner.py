"""
一次生成的完整编排

调用方（客户端 sidecar）只需要构造 ExportRequest，拿到 ExportResult：
打开模板 → 替换两组文字 → 导生成片 → 关闭文档。
任何一步失败都返回结构化结果，不抛异常到上层。
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List

from .export_check import inspect_image, sweep_stale_temps, temp_output_path
from .ps_service import get_ps_service

logger = logging.getLogger(__name__)

# 模板里约定的两个占位符。用固定占位符而不是"图层名"，员工改模板时
# 只要占位符还在，生成就不会错位——这是这套方案能跑稳的关键。
DEFAULT_AUTHOR_PLACEHOLDER = "{{作者}}"
DEFAULT_TITLE_PLACEHOLDER = "{{标题}}"

# 成品图命名规则（员工在客户端设置里选）
#   author       李四.jpg
#   author_title 李四_基于深度学习的图像分割方法研究.jpg
FILENAME_RULE_AUTHOR = "author"
FILENAME_RULE_AUTHOR_TITLE = "author_title"
FILENAME_RULES = (FILENAME_RULE_AUTHOR, FILENAME_RULE_AUTHOR_TITLE)
DEFAULT_FILENAME_RULE = FILENAME_RULE_AUTHOR_TITLE

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
    # 成品图命名规则：author / author_title
    filename_rule: str = DEFAULT_FILENAME_RULE


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
    """输出路径，按设置的命名规则生成。

    不再默认带时间戳：员工要的是"一眼能在文件夹里找到某个人的图"，
    `李四_20261021_183343.jpg` 找起来反而费劲。重名由 _unique_path 兜底加序号，
    绝不覆盖已有文件（覆盖等于悄悄丢掉上一次的成品）。
    """
    suffix = "jpg" if req.output_format.upper() in ("JPG", "JPEG") else req.output_format.lower()
    rule = (req.filename_rule or DEFAULT_FILENAME_RULE).strip().lower()
    if rule not in FILENAME_RULES:
        rule = DEFAULT_FILENAME_RULE

    parts = [safe_filename(req.author)]
    if rule == FILENAME_RULE_AUTHOR_TITLE:
        parts.append(safe_filename(req.title))
    stem = "_".join(p for p in parts if p)
    return _unique_path(Path(req.output_dir), stem, suffix)


def _unique_path(base: Path, stem: str, suffix: str) -> Path:
    """同名不覆盖：已存在就加 (2)、(3)……

    极端情况（同一作者上千张）退回时间戳，仍然不覆盖。
    """
    target = base / f"{stem}.{suffix}"
    if not target.exists():
        return target
    for i in range(2, 1000):
        candidate = base / f"{stem}({i}).{suffix}"
        if not candidate.exists():
            return candidate
    return base / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.{suffix}"


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

    # 上次被取消/崩溃留下的半成品（临时名）先清掉，别越积越多
    sweep_stale_temps(output_dir)

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
        # 导出前体检：尺寸拿来做成品比对；CMYK/16bit 会导出偏色，先转过来
        info = service.document_info() if hasattr(service, "document_info") else {}
        expect_size = (info["width"], info["height"]) if info.get("width") and info.get("height") else None
        ok, prep_warnings = service.prepare_for_export() if hasattr(service, "prepare_for_export") else (True, [])
        if not ok:
            return done(False, message="没有打开的文档")

        # 真实模板（录用通知 PSD）里占位符是嵌在整段文字中的，例如：
        #   "姓名同志：您（你们）撰写的文章《草莓种植》经专家指导委员会……"
        # 必须用「包含」替换（文字替换文字）。用「精确」全等匹配永远命中不了，
        # 会直接踩零匹配治理判失败——员工侧表现是"一点生成就报错"。
        # {{作者}} 这类独立占位符在包含模式下同样生效，两种模板写法都兼容。
        replacements = [
            {"find": req.author_placeholder, "replace": author, "regex": False, "match_mode": "包含"},
            {"find": req.title_placeholder, "replace": title, "regex": False, "match_mode": "包含"},
        ]
        ok, _, error = service.apply_text_replacements(replacements)
        warnings = list(getattr(service, "last_replacement_warnings", []) or [])
        if not ok:
            return done(False, message=error, warnings=warnings)
        warnings = list(prep_warnings) + warnings

        final_path = build_output_path(req)
        temp_path = temp_output_path(final_path)
        ok, error = service.export_document(temp_path, req.output_format, req.jpg_quality)
        if not ok:
            # 导出失败时临时名也不该留着（员工看到 .psluyong-tmp 会来问）
            _discard(temp_path)
            return done(False, message=error, warnings=warnings)

        # 金标准校验：尺寸/空白/色域/半截文件。**验过了才用交付名**，
        # 所以"目录里有个名字合规但内容是半截的图"在结构上不可能发生
        check = inspect_image(temp_path, expect_size=expect_size)
        if not check.ok:
            kept = _reject(temp_path, final_path, keep=check.keep)
            message = check.reason
            if kept is not None:
                message += f"。这张图已另存为「{kept.name}」，可打开核对"
            logger.error("成品校验未通过（%s）: %s", check.detail, message)
            return done(False, message=message, warnings=warnings)

        try:
            os.replace(temp_path, final_path)
        except OSError as e:
            _discard(temp_path)
            return done(False, message=f"成品发布失败（{final_path}）: {e}", warnings=warnings)

        logger.info("成品已发布 %s（%s）", final_path, check.detail)
        return done(True, image_path=final_path, warnings=warnings)

    finally:
        # 无论成功失败都要关掉文档，否则下次打开会叠文档、PS 内存越用越大
        service.close_document(save_changes=False)


def _discard(path: Path) -> None:
    """删掉本次没用的产物（临时文件/残缺文件），删不掉也不影响返回给员工的结果"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as e:
        logger.warning("清理临时文件失败 %s: %s", path, e)


def _reject(temp_path: Path, final_path: Path, keep: bool) -> Path | None:
    """校验未通过：成型但可疑的图改名留档（给员工看），垃圾直接删。"""
    if not keep:
        _discard(temp_path)
        return None
    try:
        target = _unique_path(final_path.parent, f"{final_path.stem}.校验未通过", final_path.suffix.lstrip("."))
        os.replace(temp_path, target)
        return target
    except OSError as e:
        logger.warning("留档校验失败的成品时出错 %s: %s", temp_path, e)
        _discard(temp_path)
        return None
