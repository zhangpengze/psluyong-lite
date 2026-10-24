"""文本匹配 / 替换 / 替换后检查的纯逻辑（ps_service 与 ps_connector 共用）

从两个模块拆出：匹配与替换算法完全一致（换行归一、字面量反向引用），
复制两份迟早漂移——收拢到一处。无 PS 状态、无 I/O。
"""

from __future__ import annotations

import re
from typing import Any, List


def match_text(source: str, pattern: str, regex: bool, match_mode: str) -> bool:
    """判断文本是否命中规则。PS 的换行常是 \\r，统一成 \\n 再比对，
    否则「精确」永远匹配不上。"""
    source_norm = source.replace("\r\n", "\n").replace("\r", "\n")
    pattern_norm = pattern.replace("\r\n", "\n").replace("\r", "\n")
    if regex:
        return re.search(pattern, source) is not None
    if match_mode == "精确":
        return source_norm == pattern_norm
    return pattern_norm in source_norm


def replace_text(
    source: str, pattern: str, replacement: str, regex: bool, match_mode: str
) -> str:
    """按规则替换文本。"""
    if regex:
        # 用 lambda 返回字面量：用户输入里的 \1、\g<name> 不会被 re.sub 当成反向引用
        return re.sub(pattern, lambda m: replacement, source)
    if match_mode == "精确":
        return replacement
    return source.replace(pattern, replacement)


def safe_layer_name(layer: Any) -> str:
    """取图层名做告警文案；属性读不出时兜底「未知图层」，不让告警自身炸掉"""
    try:
        return str(getattr(layer, "Name", "未知图层"))
    except Exception:  # noqa: BLE001
        return "未知图层"


def check_text_overflow(
    document: Any,
    layer: Any,
    text_item: Any,
    available_fonts: "set[str] | None",
    warnings: List[str],
) -> None:
    """替换后检查字体可用性与文本是否溢出画布，只告警不阻断。"""
    layer_name = safe_layer_name(layer)
    try:
        if available_fonts is not None and str(text_item.Font) not in available_fonts:
            warnings.append(f"图层「{layer_name}」字体不可用，请检查替代字体")
    except Exception:  # noqa: BLE001
        warnings.append(f"图层「{layer_name}」字体未验证")

    try:
        bounds = [float(v) for v in layer.Bounds]
        width = float(document.Width)
        height = float(document.Height)
        if bounds[0] < 0 or bounds[1] < 0 or bounds[2] > width or bounds[3] > height:
            warnings.append(f"图层「{layer_name}」文本超出画布")
        if int(text_item.Kind) == 2:
            warnings.append(f"图层「{layer_name}」段落框内部溢出未验证，请打开成品图确认")
    except Exception:  # noqa: BLE001
        warnings.append(f"图层「{layer_name}」文本边界未验证")
