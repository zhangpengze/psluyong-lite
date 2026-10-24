"""文档级替换流程（PSService.apply_text_replacements 的实现，拆出降行数 & 可单测）。

流程本身：递归下钻图层组 → 找 Kind==2 文本图层 → 逐条规则匹配替换 →
统计命中情况 → 决定成功/失败。

三道治理，都是"别让假成功出去"：
  1. 零匹配：一条规则都没命中 = 导出的是模板本身 → 失败
  2. 必填占位符没命中：比如模板里 `{{标题}}` 被删了 → 失败（旧实现只告警）
  3. 正则写错：只告警并跳过该规则，不拖垮整篇（是规则语法问题，不是模板问题）
"""

from __future__ import annotations

import logging
import re
from typing import Any, List

from .ps_com_errors import format_com_error
from .ps_rules import optional_missing_warnings, required_miss_message, required_missing
from .ps_text import check_text_overflow, match_text, replace_text, safe_layer_name

logger = logging.getLogger(__name__)

try:  # pywin32 只在 Windows 装；非 Windows 环境走不到 COM 分支
    import pywintypes
except ImportError:  # pragma: no cover - 非 Windows
    pywintypes = None  # type: ignore[assignment]


def apply_replacements(
    document: Any,
    replacements: List[dict],
    warnings: List[str],
) -> "tuple[bool, str | None, str]":
    """对 document 全部文本图层应用替换规则。

    Returns:
        (是否成功, 第一个替换文本（用于文件名）, 错误信息)

    warnings 由调用方持有并在调用后读取（同一列表，原地追加）。
    """
    first_replacement_text: "str | None" = None
    changed_layer_count = 0
    matched_rules: set[int] = set()
    # 正则写错的规则：它们"没命中"是语法问题，不算模板缺占位符
    invalid_rules: set[int] = set()

    try:
        try:
            available_fonts = {str(f.PostScriptName) for f in document.Application.Fonts}
        except Exception:  # noqa: BLE001 - 取不到字体表不阻断生成，只降级为告警
            available_fonts = None
            warnings.append("字体可用性未验证，请打开成品图确认")

        def process_layers(layers_obj: Any) -> None:
            nonlocal first_replacement_text, changed_layer_count

            for layer in layers_obj:
                if getattr(layer, "TypeName", "") == "LayerSet":
                    process_layers(layer.Layers)
                    continue
                # Kind == 2 表示文本图层
                if getattr(layer, "Kind", None) != 2:
                    continue

                try:
                    text_item = layer.TextItem
                    original = str(text_item.Contents)
                    new_text = original

                    for rule_index, cfg in enumerate(replacements):
                        find = str(cfg.get("find", ""))
                        replace = str(cfg.get("replace", ""))
                        use_regex = bool(cfg.get("regex", False))
                        match_mode = str(cfg.get("match_mode", "精确"))
                        if not find or rule_index in invalid_rules:
                            continue
                        try:
                            if match_text(new_text, find, use_regex, match_mode):
                                matched_rules.add(rule_index)
                                new_text = replace_text(
                                    new_text, find, replace, use_regex, match_mode
                                )
                                if first_replacement_text is None and replace:
                                    first_replacement_text = replace
                        except re.error as e:
                            invalid_rules.add(rule_index)
                            warning = (
                                f"图层「{safe_layer_name(layer)}」正则规则无效，已跳过: "
                                f"{find!r} ({e})"
                            )
                            warnings.append(warning)
                            logger.warning(warning)

                    if new_text != original:
                        text_item.Contents = new_text
                        changed_layer_count += 1
                        check_text_overflow(
                            document, layer, text_item, available_fonts, warnings
                        )

                except (AttributeError, TypeError):
                    continue

        process_layers(document.Layers)

        warnings.extend(optional_missing_warnings(replacements, matched_rules))
        missing = required_missing(replacements, matched_rules, skip=invalid_rules)

        # 零匹配治理：没有任何图层被替换时不能返回成功，
        # 否则会导出一张与模板一模一样的图，员工以为做完了。
        if changed_layer_count == 0:
            logger.warning("文本替换零匹配：未找到任何可替换的文本")
            return False, None, "未找到任何可替换的文本，请检查模板占位符与查找规则是否一致"

        # 必填占位符没命中 = 成品缺内容。旧实现只加一条告警照样交付，
        # 于是"标题被删"的模板会导出一张没有标题的录用通知，还计入今日出图。
        if missing:
            logger.warning("必填占位符未命中：%s", "、".join(missing))
            return False, None, required_miss_message(missing)

        return True, first_replacement_text, ""

    except (AttributeError, RuntimeError) as e:
        logger.error("文本替换失败: %s", e)
        return False, None, f"文本替换失败: {e}"
    except Exception as e:  # noqa: BLE001 - 兜底翻译 COM 错误
        if pywintypes is not None and isinstance(e, pywintypes.com_error):
            msg = format_com_error(e)
            logger.error("文本替换 COM 错误: %s", msg)
            return False, None, f"文本替换失败: {msg}"
        logger.error("文本替换未知错误: %s", e)
        return False, None, f"文本替换失败: {e}"
