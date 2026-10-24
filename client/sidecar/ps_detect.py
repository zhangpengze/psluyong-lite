"""自动识别本机安装的 Photoshop：任意版本、任意盘符。

为什么不让员工手填路径：COM 连接走的是版本无关的 ProgID
（``Photoshop.Application``），CS6 到 2025 装哪版都能连。路径只在两处用得上——
读 exe 的文件版本写进记录、以及在设置里展示。就为这两件事让员工记住
"Adobe Photoshop 2024" 装到哪个盘，是一道白花的门槛（真实踩坑：
有人装在 D 盘，扫描 C 盘找不到，于是设置里永远空着）。

识别顺序（都找不到才轮到员工手动填）：

1. 设置里手填的路径 —— 员工明确指定时以他为准
2. 注册表 ``App Paths\\Photoshop.exe`` —— Adobe 自己注册的"默认 PS"，
   CS6 / CC / 2020+ 全都有，且不受装在哪个盘影响
3. 卸载信息里的 ``InstallLocation``（DisplayName 含 Adobe Photoshop，
   排除 Elements：那不是我们要自动化的东西）
4. 常见安装目录扫描 —— 兜底，给手工挪过目录、注册表被清理过的机器

多个候选时取**版本最高**的那个：COM 的 ProgID 解析到的通常也是最新版，
两者保持一致，员工看到的才和实际驱动 PS 的是同一个。

非 Windows / 读不到注册表都不抛错，返回 ``found=False``——
识别不出就让设置页显示"未识别"，由员工决定，而不是让生成流程崩在探测上。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Tuple

logger = logging.getLogger(__name__)

# 常见的 Photoshop 安装根。只放"几乎不会变"的几处，
# 具体版本号交给目录名匹配（见 _INSTALL_DIR_RE）。
_SCAN_ROOTS: Tuple[str, ...] = (
    r"C:\Program Files\Adobe",
    r"C:\Program Files (x86)\Adobe",
    r"D:\Program Files\Adobe",
    r"D:\Program Files (x86)\Adobe",
)

# 目录名里能认出是 Photoshop 的写法：2024 / CC 2015 / CS6 / CC
_INSTALL_DIR_RE = re.compile(
    r"photoshop\s*((?:cc\s*)?(?:\d{4}|cs\s*\d+|cc))\b", re.IGNORECASE
)
# "CS6" → 2012、"CC" → 2013，用于把老版本号排到正确的位置
_CS_YEAR = {"cs1": 2002, "cs2": 2004, "cs3": 2006, "cs4": 2008, "cs5": 2010, "cs6": 2012, "cc": 2013}


@dataclass
class PSInstall:
    """识别结果。found=False 时 path/version 为空串。

    override_missing：员工手填的路径不存在。这时不假装"没装 PS"——
    会自动识别顶上（生成不该被一个失效的手填值卡住），但把真相带回去，
    让设置页能提示一句"你填的路径已失效"。
    """

    path: str = ""
    version: str = ""
    source: str = "none"  # config | app-paths | uninstall | scan | none
    override_missing: bool = False

    @property
    def found(self) -> bool:
        return bool(self.path)

    def to_dict(self) -> dict:
        """给 /status 用：source 顺手翻成人话，前端不用再维护一张映射表。"""
        return {
            "path": self.path,
            "version": self.version,
            "source": self.source,
            "source_label": describe_source(self.source),
            "found": self.found,
            "override_missing": self.override_missing,
        }


# ========== 纯函数（可单测，不碰注册表）==========


def _year_from_name(name: str) -> int:
    """从 'Adobe Photoshop 2024' / 'CS6' / 'CC 2015' 里取一个可排序的年份。

    返回 0 表示认不出来。排序用，不需要精确到月份。
    """
    m = re.search(r"(20\d{2})", name)
    if m:
        return int(m.group(1))
    m = re.search(r"\bcs\s*(\d+)\b", name, re.IGNORECASE)
    if m:
        return _CS_YEAR.get(f"cs{m.group(1)}", 0)
    if re.search(r"\bcc\b", name, re.IGNORECASE):
        return _CS_YEAR["cc"]
    return 0


def _file_version(path: str) -> str:
    """读 exe 的文件版本（13.0.0 这种）。读不到就返回空串。"""
    if not path or not os.path.exists(path):
        return ""
    try:
        import win32api  # type: ignore

        info = win32api.GetFileVersionInfo(path, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}"
    except Exception:  # noqa: BLE001 - 读不到版本不影响识别本身
        return ""


def version_sort_key(path: str) -> Tuple[int, ...]:
    """候选之间的排序键：文件版本优先，退到目录名里的年份。

    为什么需要两套：装在 D 盘的绿色版/精简版可能读不到文件版本信息，
    这时至少还能按 "Adobe Photoshop 2024" 这种目录名排。
    元组长度不齐也能比（Python 逐项比，短的先尽先小），不用补齐。
    """
    file_ver = _file_version(path)
    if file_ver:
        nums = tuple(int(x) for x in file_ver.split(".") if x.isdigit())
        if nums:
            return nums
    return (_year_from_name(os.path.basename(os.path.dirname(path))), 0, 0)


def pick_best(candidates: Iterable[str]) -> str:
    """多个候选里挑版本最高的；全认不出年份时返回第一个存在的。"""
    existing = [c for c in candidates if c and os.path.exists(c)]
    if not existing:
        return ""
    return max(existing, key=version_sort_key)


def is_photoshop_exe(path: str) -> bool:
    return bool(path) and os.path.basename(path).lower() == "photoshop.exe"


# ========== 采集候选（碰系统，Windows 专属）==========


def _from_app_paths() -> List[str]:
    """App Paths\\Photoshop.exe 的默认值：Adobe 注册的"默认 PS"。"""
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return []
    out: List[str] = []
    # 64 位 PS 写在 WOW6432Node 下，32 位写在上层，两处都要看
    keys = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe",
    ]
    for key in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                value = str(winreg.QueryValue(k, None) or "")
        except OSError:
            continue
        if is_photoshop_exe(value):
            out.append(value)
    return out


def _from_uninstall() -> List[str]:
    """卸载信息里的 InstallLocation。DisplayName 必须含 Adobe Photoshop。"""
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return []
    out: List[str] = []
    bases = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for base in bases:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
                count = winreg.QueryInfoKey(root)[0]
                for i in range(count):
                    sub = winreg.EnumKey(root, i)
                    try:
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{sub}") as k:
                            name = str(winreg.QueryValueEx(k, "DisplayName")[0])
                            if "photoshop" not in name.lower():
                                continue
                            # Elements 是另一款软件，没有 Photoshop.Application COM
                            if "elements" in name.lower():
                                continue
                            install = str(winreg.QueryValueEx(k, "InstallLocation")[0] or "")
                    except OSError:
                        continue
                    if not install:
                        continue
                    candidate = os.path.join(install, "Photoshop.exe")
                    if is_photoshop_exe(candidate):
                        out.append(candidate)
        except OSError:
            continue
    return out


def _from_scan() -> List[str]:
    """扫常见安装根。装在别的盘就只能靠注册表那两条路了。"""
    out: List[str] = []
    for root in _SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not _INSTALL_DIR_RE.search(name):
                continue
            candidate = os.path.join(root, name, "Photoshop.exe")
            if is_photoshop_exe(candidate):
                out.append(candidate)
    return out


# 默认采集顺序：越靠前越可信（见模块 docstring）
DEFAULT_COLLECTORS: Tuple[Tuple[str, Collector], ...] = (
    ("app-paths", _from_app_paths),
    ("uninstall", _from_uninstall),
    ("scan", _from_scan),
)


def detect_with(
    collectors: Iterable[Tuple[str, Collector]], explicit: str = ""
) -> PSInstall:
    """detect_photoshop 的可测版本：候选来源由外部注入（不需要真装 PS）。"""
    explicit = (explicit or "").strip().strip('"')
    if explicit:
        if is_photoshop_exe(explicit) and os.path.exists(explicit):
            return PSInstall(explicit, _file_version(explicit), "config")
        # 手填的路径不存在：不拿它当结果，但也不装作没事——
        # 自动识别照常顶上，override_missing 让设置页能提醒一句
        for source, collect in collectors:
            best = pick_best(collect())
            if best:
                return PSInstall(best, _file_version(best), source, override_missing=True)
        return PSInstall("", "", "none", override_missing=True)

    for source, collect in collectors:
        best = pick_best(collect())
        if best:
            return PSInstall(best, _file_version(best), source)
    return PSInstall()


def detect_photoshop(explicit: str = "") -> PSInstall:
    """识别本机 Photoshop。explicit 是设置里手填的路径，优先以它为准。"""
    return detect_with(DEFAULT_COLLECTORS, explicit)


def describe_source(source: str) -> str:
    """把来源翻成人话，设置页显示"怎么找到的"，员工能自己判断可不可信。"""
    return {
        "config": "手动指定",
        "app-paths": "系统注册表识别",
        "uninstall": "安装信息识别",
        "scan": "安装目录扫描",
        "none": "未识别",
    }.get(source, source)
