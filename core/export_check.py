"""导出成品的金标准校验：尺寸 / 空白 / 色域 / 半截文件

原来唯一的校验是「文件存在且大小 > 0」（ps_service.export_document）。那挡不住
四类假成功，而它们全都会被记成 success、计入"今日出图"与成功率：

  1. **尺寸不对**：模板被裁过、文档尺寸改了，产出 1×1 或 1024×100 的图
  2. **几乎纯色**：文字层没渲染出来（字体缺失/图层被隐藏），白底一片
  3. **半截文件**：SaveAs 中途被取消或 PS 崩了，留下一个文件名完全合规的残缺 JPG
  4. **偏色**：CMYK / 16bit 模板直接导 JPG，绝大多数看图软件显示失真

校验放在**发布之前**：先写 `X.psluyong-tmp.jpg`，验过了才 rename 成交付名
`X.jpg`。所以「交付目录里出现一个名字合规但内容是半截的文件」在结构上不可能——
取消/崩溃只会留下临时名，由 sweep_stale_temps 在下次生成时清掉。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 小于这个字节数的图不可能是正常成品（800×600 的空 JPG 也有 5KB 上下）
MIN_BYTES = 2048
# 灰度极差小于这个值视为"几乎纯色"：录用通知有文字，正常图极差远超 5
MIN_UNIFORM_RANGE = 5
# 允许的成品色彩模式：RGB 正常；L（灰度）是合法模板，不算偏色。CMYK/16bit 一律拦
ALLOWED_MODES = frozenset({"RGB", "L"})
# 临时文件标记：`作者_标题.psluyong-tmp.jpg`。带 .psluyong-tmp 的绝对不是交付物
TEMP_INFIX = ".psluyong-tmp"
STALE_TEMP_AGE_S = 3600.0


@dataclass(frozen=True)
class ImageCheck:
    """校验结果。keep：失败时是否保留这张图（可疑但成型的留给员工看，垃圾删掉）"""

    ok: bool
    reason: str = ""
    keep: bool = False
    detail: str = ""


def temp_output_path(final_path: Path) -> Path:
    """临时路径沿用交付名的扩展名：PS 靠扩展名+SaveOptions 决定格式，不能改后缀"""
    p = Path(final_path)
    return p.with_name(f"{p.stem}{TEMP_INFIX}{p.suffix}")


def inspect_image(path: Path, expect_size: tuple[int, int] | None = None) -> ImageCheck:
    """校验一张成品图。任何异常都不外抛——校验自己炸掉比放行一张坏图更糟。"""
    p = Path(path)
    try:
        size_bytes = p.stat().st_size
    except OSError as e:
        return ImageCheck(False, "导出文件读不到（可能被别的程序占用或已被删除），请重试", detail=str(e))

    if size_bytes < MIN_BYTES:
        # 0 字节 / 几百字节：PS 保存被打断，删掉，别让员工误当成品
        return ImageCheck(
            False,
            f"导出文件只有 {size_bytes} 字节，明显不完整（保存被中断），请重试",
            keep=False,
            detail=f"size={size_bytes}",
        )

    try:
        from PIL import Image
    except ImportError:  # 没装 Pillow 时退回旧口径，绝不因为缺依赖让所有人出不了图
        logger.warning("未安装 Pillow，跳过成品内容校验: %s", p)
        return ImageCheck(True, detail="pillow-missing")

    try:
        with Image.open(p) as img:
            img.load()  # 完整解码：半截 JPG 只在 load() 时才炸，verify() 看不出来
            mode = img.mode
            width, height = img.size
            low, high = img.convert("L").getextrema()
    except Exception as e:  # noqa: BLE001 - 解不开就是坏文件
        return ImageCheck(
            False,
            "导出文件不完整或不是有效图片（PS 在保存过程中被中断），已删除，请重试",
            keep=False,
            detail=f"{type(e).__name__}: {e}",
        )

    detail = f"{width}×{height} {mode} {size_bytes}B"
    if mode not in ALLOWED_MODES:
        return ImageCheck(
            False,
            f"成品是 {mode} 色彩模式的图，打开会偏色（模板应是 RGB / 8bit）",
            keep=True,
            detail=detail,
        )
    if expect_size:
        ew, eh = int(expect_size[0]), int(expect_size[1])
        if (width, height) != (ew, eh) and (width, height) != (eh, ew):
            return ImageCheck(
                False,
                f"成品尺寸 {width}×{height} 与模板 {ew}×{eh} 不一致（模板可能被裁过或画布尺寸变了）",
                keep=True,
                detail=detail,
            )
    if high - low < MIN_UNIFORM_RANGE:
        return ImageCheck(
            False,
            "成品几乎是纯色（文字或图层没渲染出来），请检查字体是否安装、图层是否被隐藏",
            keep=True,
            detail=f"{detail} 灰度范围={low}-{high}",
        )
    return ImageCheck(True, detail=detail)


def sweep_stale_temps(directory: Path, older_than_s: float = STALE_TEMP_AGE_S) -> int:
    """清掉上一次被中断（取消 / PS 崩溃）留下的临时文件。

    只碰自己造的名字（含 .psluyong-tmp），且超过 1 小时才算陈旧——
    员工输出目录里的一律不动。
    """
    base = Path(directory)
    if not base.is_dir():
        return 0
    now = time.time()
    removed = 0
    for path in base.glob(f"*{TEMP_INFIX}*"):
        try:
            # 夹零：Windows 上 mtime 偶尔比 time.time() 大几微秒，直接相减会变成负数
            if max(0.0, now - path.stat().st_mtime) < older_than_s:
                continue
            path.unlink()
            removed += 1
        except OSError as e:
            logger.debug("清理临时文件失败 %s: %s", path, e)
    if removed:
        logger.info("清理了 %d 个上次未完成的临时文件（%s）", removed, base)
    return removed
