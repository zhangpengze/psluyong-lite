"""COM 错误 → 人话翻译（从 ps_service 拆出的纯逻辑）

  - HRESULT 精确表：32 位有符号值，与 com_error.hresult 一致
  - 关键字规则：excepinfo 描述 + 原始消息 contains，按序命中即返回。
    顺序有讲究：具体场景（暂存盘/字体/许可证）必须排在通用场景（disk/not found）之前，
    顺序错了会把「暂存盘满」报成「磁盘空间不足」，员工清理错地方。

无 I/O、无 PS 依赖，纯函数。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# 常见 HRESULT → 员工能照着做的动作（32 位有符号值，与 com_error.hresult 一致）
_COM_ERROR_MESSAGES: Dict[int, str] = {
    -2147221005: "Photoshop 未运行，请先启动 Photoshop 再点执行",
    -2147221021: "无法创建 Photoshop COM 对象，请检查 Photoshop 是否安装完整",
    -2147417848: "Photoshop 进程已终止或无响应，请重启 Photoshop 后重试",
    -2147467259: "Photoshop 操作失败或超时，请重试；若反复出现请重启 Photoshop",
    -2147220995: "Photoshop 服务不可用，请重启 Photoshop 后重试",
    -2147024864: "文件被占用，请关闭占用该模板文件的程序后重试",  # 0x80070020
    -2147024784: "磁盘空间已满，请清理磁盘空间后重试",  # 0x80070070
    -2147024891: "没有权限访问该文件，请关闭占用程序或以管理员身份运行后重试",  # 0x80070005
    -2147024894: "文件不存在，请检查文件路径是否正确",  # 0x80070002
    -2147024893: "路径不存在，请检查输出目录设置是否正确",  # 0x80070003
    -2147418111: "Photoshop 正忙（可能正在启动或有弹窗未处理），请稍候再重试",  # 0x80010001
    -2147023170: "与 Photoshop 的连接已断开（进程可能崩溃），请重启 Photoshop 后重试",  # 0x800706BE
    -2146959355: "无法启动 Photoshop，请手动打开 Photoshop 后再点执行",  # 0x80080005
}

# excepinfo / 错误文本关键字 → 白话指引（小写 contains，按序命中即返回）。
# 顺序有讲究：具体场景（暂存盘/字体/许可证）必须排在通用场景（disk）之前。
_COM_ERROR_KEYWORD_RULES: List[Tuple[Tuple[str, ...], str]] = [
    (
        ("scratch disk", "scratch", "暂存盘"),
        "Photoshop 暂存盘空间不足，请清理磁盘空间或在 Photoshop 中更换暂存盘位置后重试",
    ),
    (
        ("font", "字体"),
        "缺少字体，请安装模板所用字体或确认 Photoshop 能正常加载字体后重试",
    ),
    (
        (
            "license",
            "licen",
            "activated",
            "activation",
            "serial",
            "trial",
            "许可",
            "激活",
            "授权",
            "订阅",
        ),
        "Photoshop 未激活或许可证已过期，请先打开 Photoshop 完成登录/激活后再点执行",
    ),
    (
        ("sharing violation", "in use", "being used", "locked", "占用", "正在使用"),
        "文件被占用，请关闭占用该模板文件的程序后重试",
    ),
    (
        ("disk full", "no space", "not enough space", "磁盘", "空间不足"),
        "磁盘空间不足，请清理磁盘空间后重试",
    ),
    (
        ("could not be found", "not found", "找不到", "不存在"),
        "文件不存在，请检查文件路径是否正确",
    ),
    (("out of memory", "内存不足"), "内存不足，请关闭其他占用内存的程序后重试"),
    (("not responding", "无响应"), "Photoshop 无响应，请重启 Photoshop 后重试"),
]


def _extract_com_error_text(error: Any) -> str:
    """汇总 COM 异常中可用于关键字匹配的文本（excepinfo 描述 + 原始消息）"""
    parts: List[str] = [str(error)]
    # pywintypes.com_error.args = (hresult, message, excepinfo, ...)
    for arg in getattr(error, "args", None) or ():
        if isinstance(arg, str):
            parts.append(arg)
        elif isinstance(arg, (tuple, list)):
            # excepinfo 元组：(source, description, helpfile, helpcontext, ...)
            parts.extend(item for item in arg if isinstance(item, str))
    return " ".join(parts)


def format_com_error(error: Any) -> str:
    """COM 错误 → 人话。HRESULT 精确命中优先，其次关键字 contains。"""
    hresult = getattr(error, "hresult", 0)
    if hresult in _COM_ERROR_MESSAGES:
        return _COM_ERROR_MESSAGES[hresult]

    text = _extract_com_error_text(error).lower()
    for keywords, friendly in _COM_ERROR_KEYWORD_RULES:
        if any(keyword in text for keyword in keywords):
            return friendly
    return f"COM 错误 (代码: {hresult}): {error}"
