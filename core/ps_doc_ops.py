"""文档级文本替换流程（ps_connector.apply_text_replacements 的实现，拆出降行数）

对一篇文档递归应用替换规则：图层组下钻、Kind==2 文本图层、坏规则警告、
坏图层跳过不中断。返回首个替换文本（供成品文件命名），警告经传入的
warnings 列表回填（调用方负责挂到实例上）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import List

from .ps_text import match_text, replace_text

logger = logging.getLogger(__name__)


def apply_document_replacements(
    document,
    replacements: Iterable[Mapping[str, object]],
    warnings: List[str],
) -> str | None:
    """递归替换 document 全部文本图层，返回首个非空替换文本（用于文件命名）。

    替换失败的图层记录进 warnings（同时写日志），不静默吞掉；
    单个坏图层（缺属性 / 意外异常）跳过，不中断整篇处理。
    """
    first_replacement_text: str | None = None

    def process_layers(layers_obj):
        """递归遍历图层组，对文本图层应用替换规则。"""
        nonlocal first_replacement_text
        for layer in layers_obj:
            # 检查是否为 LayerSet (Group)，如果是则递归
            if getattr(layer, "TypeName", "") == "LayerSet":
                process_layers(layer.Layers)
                continue

            # Kind == 2 表示文本图层
            if getattr(layer, "Kind", None) != 2:
                continue

            try:
                text_item = layer.TextItem
                # Explicitly convert to string to avoid COM object issues
                original = str(text_item.Contents)
                new_text = original

                for cfg in replacements:
                    find = str(cfg.get("find", ""))
                    replace = str(cfg.get("replace", ""))
                    regex = bool(cfg.get("regex", False))
                    match_mode = str(cfg.get("match_mode", "精确"))

                    if not find:
                        continue

                    try:
                        if match_text(new_text, find, regex, match_mode):
                            new_text = replace_text(
                                new_text, find, replace, regex, match_mode
                            )
                            if first_replacement_text is None and replace:
                                first_replacement_text = replace
                    except re.error as e:
                        # 非法正则：记录失败图层到警告列表，不再静默吞掉
                        layer_name = getattr(layer, "Name", "未知图层")
                        warning = f"图层「{layer_name}」正则规则无效，已跳过: {find!r} ({e})"
                        warnings.append(warning)
                        logger.warning(warning)

                if new_text != original:
                    text_item.Contents = new_text

            except (AttributeError, TypeError) as e:
                # 图层属性访问错误或类型错误，记录但继续处理其他图层
                logger.error(f"处理图层 {getattr(layer, 'Name', 'Unknown')} 时出错: {e}")
            except Exception as e:
                # 其他未预期的错误，记录但继续
                logger.error(f"处理图层 {getattr(layer, 'Name', 'Unknown')} 时发生未预期错误: {e}")

    # 从文档顶层图层开始
    process_layers(document.Layers)
    return first_replacement_text
