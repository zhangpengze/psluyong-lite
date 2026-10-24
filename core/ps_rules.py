"""替换规则的命中覆盖检查（从 ps_service 抽出，纯逻辑、可单测）。

为什么"有一条规则没命中"不能只当警告：

    员工填了作者和标题，模板里两个占位符都该被替换。`{{标题}}` 被人误删之后，
    旧实现只在"所有规则都没命中"时才失败——只缺一个时照样 success，导出一张
    没有标题的录用通知，还计入"今日出图"。这种图交到作者手里是要出事的，
    而失败一条记录最多是重试一次。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def required_missing(
    replacements: Iterable[Mapping[str, Any]], matched: set[int], skip: set[int] | None = None
) -> list[str]:
    """必须命中却没命中的占位符原文（按规则顺序）。

    `required` 缺省为 True：带 replace 值的规则按定义就该出现在模板里。
    确实有不带标题的模板时，调用方显式传 "required": False 即可降级为告警。

    `skip` 里的规则不参与判定（正则写错的规则：那是规则语法问题，
    归"正则规则无效"那条告警管，不该被说成"模板里找不到这个占位符"）。
    """
    ignore = skip or set()
    missing: list[str] = []
    for index, cfg in enumerate(replacements):
        find = str(cfg.get("find", "") or "")
        if not find or index in matched or index in ignore:
            continue
        if bool(cfg.get("required", True)):
            missing.append(find)
    return missing


def optional_missing_warnings(
    replacements: Iterable[Mapping[str, Any]], matched: set[int]
) -> list[str]:
    """非必填规则没命中时的提示（必填的由调用方直接判失败，不再重复告警）。"""
    warnings: list[str] = []
    for index, cfg in enumerate(replacements):
        find = str(cfg.get("find", "") or "")
        if not find or index in matched or bool(cfg.get("required", True)):
            continue
        warnings.append(f"第 {index + 1} 条替换规则未命中，请核对成品")
    return warnings


def required_miss_message(missing: list[str]) -> str:
    """给员工看的话：说清是哪个占位符、去哪改，别只说"失败"。"""
    names = "、".join(f"「{find}」" for find in missing)
    return (
        f"模板里找不到占位符 {names}。\n"
        "逐字核对模板 PSD 里这处文字还在不在（可能被删掉或改过写法），"
        "以及设置里的占位符写法与模板是否完全一致。"
    )
