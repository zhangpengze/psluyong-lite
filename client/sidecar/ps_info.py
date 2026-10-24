"""本机 Photoshop 定位与版本（拆自 server.py）。

识别结果只用于两件事——给记录写 ps_version、以及在设置页展示；
COM 连接本身用版本无关的 ProgID，识别不到也不拦生成。
"""

from __future__ import annotations

import os

from .ps_detect import PSInstall, detect_photoshop


def _ps_version(ps_path: str) -> str:
    """读 Photoshop.exe 的文件版本，读不到就留空（不影响生成）"""
    if not ps_path or not os.path.exists(ps_path):
        return ""
    try:
        import win32api  # type: ignore

        info = win32api.GetFileVersionInfo(ps_path, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}"
    except Exception:  # noqa: BLE001
        return ""


def _resolve_ps(cfg) -> PSInstall:
    """当前机器上的 Photoshop：手填优先，否则自动识别（任意版本/任意盘符）。

    识别结果只用于两件事——给记录写 ps_version、以及在设置页展示。
    COM 连接本身用版本无关的 ProgID，装哪版都能连，所以识别不到
    也不该拦住生成（员工可以手动指定路径）。
    """
    return detect_photoshop(getattr(cfg, "ps_path", "") or "")
