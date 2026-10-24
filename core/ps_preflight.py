"""导出前体检：读文档尺寸/色彩模式/位深，并把偏色隐患摆正

从 ps_service 拆出来的原因：这几步只跟 COM 文档对象的属性打交道，和
"连接/打开/替换/导出"的生命周期无关，单独放一处才好读。PSService 保留同名
方法做转发，调用方（runner）不需要知道实现搬过家。

为什么必须先体检：
  - 尺寸是成品校验的基准（拿不到就没法判断"图是不是被裁了"）
  - CMYK / 16bit 模板直接 SaveAs JPG，绝大多数看图软件显示偏色，
    而员工只会说"这图颜色不对"，查不出是色彩模式的问题
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# psConstants.psRGB
PS_MODE_RGB = 3


def document_info(doc: Any) -> Dict[str, int]:
    """当前文档的尺寸 / 色彩模式 / 位深。

    COM 属性读不到就留空，绝不抛——体检是"多一道保险"，不能变成新的失败源。
    """
    if doc is None:
        return {}
    info: Dict[str, int] = {}
    for key, attr in (
        ("width", "Width"),
        ("height", "Height"),
        ("mode", "Mode"),
        ("bits", "BitsPerChannel"),
    ):
        try:
            value = getattr(doc, attr, None)
        except Exception:  # noqa: BLE001 - 某个属性读不到不该影响别的
            value = None
        if isinstance(value, (int, float)):
            info[key] = int(value)
    return info


def prepare_for_export(doc: Any) -> Tuple[bool, List[str]]:
    """把色彩模式/位深摆正，返回 (是否可继续, 告警)。

    转换失败只告警不阻断——宁可给一张颜色略偏的成品，也好过整单生不出来。
    """
    if doc is None:
        return False, []
    info = document_info(doc)
    warnings: List[str] = []

    mode = info.get("mode")
    if mode is not None and mode != PS_MODE_RGB:
        try:
            doc.ChangeMode(PS_MODE_RGB)
            warnings.append(f"模板色彩模式不是 RGB（{mode}），已转成 RGB 导出，颜色可能有细微差异")
        except Exception as e:  # noqa: BLE001
            logger.warning("转 RGB 失败: %s", e)
            warnings.append(f"模板色彩模式不是 RGB（{mode}），转换失败，成品颜色可能与模板不同")

    bits = info.get("bits")
    if bits is not None and bits != 8:
        try:
            doc.BitsPerChannel = 8
            warnings.append(f"模板是 {bits} bit/通道，已降为 8 bit 导出")
        except Exception as e:  # noqa: BLE001
            logger.warning("降位深失败: %s", e)
            warnings.append(f"模板是 {bits} bit/通道，降位深失败，成品可能与模板有差异")

    return True, warnings
