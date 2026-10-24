"""客户端配置的"形状"：字段定义、默认值、写入校验

和落盘逻辑（config.py）分开放：这里只描述"配置长什么样、什么值算合法"，
完全不碰磁盘。两块以前挤在一个文件里，一改就担心碰坏另一半。

路径常量也在这里——它们是默认值的来源（比如 template_dir 默认指向
%APPDATA%/psluyong-lite/templates），落盘那边只管怎么安全地写。
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


def app_data_dir() -> Path:
    override = os.getenv("PSLUYONG_DATA_DIR")
    if override:
        return Path(override)
    base = os.getenv("APPDATA") or os.path.expanduser("~")
    return Path(base) / "psluyong-lite"


DATA_DIR = app_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
CACHE_DIR = DATA_DIR / "cache"
TEMPLATE_CACHE = CACHE_DIR / "templates"
QUEUE_DIR = DATA_DIR / "pending"
# 员工把自己的 PSD 模板放这里，客户端扫这个目录出下拉框
DEFAULT_TEMPLATE_DIR = DATA_DIR / "templates"

MAX_PARTNERS = 200
MAX_NAME_LEN = 100


@dataclass
class ClientConfig:
    server_url: str = "http://127.0.0.1:8080"
    api_key: str = ""
    employee_name: str = ""
    ps_path: str = ""
    output_dir: str = field(default_factory=lambda: str(Path.home() / "Documents" / "期刊录用"))
    jpg_quality: int = 10
    auto_clear: bool = True
    last_template_id: str = ""
    last_partner: str = ""
    # 上次生成用的作者：下次打开自动带出，同一位作者连投多篇不用重复打
    last_author: str = ""
    # 合作伙伴名单：员工自己维护，存本机，断网也能选
    partners: list[str] = field(default_factory=list)
    # PSD 模板所在目录（员工把模板文件拷进去即可）
    template_dir: str = field(default_factory=lambda: str(DEFAULT_TEMPLATE_DIR))
    # 模板里两组待替换的查找文字（全局固定，和原项目一致，在设置页修改）
    author_placeholder: str = "{{作者}}"
    title_placeholder: str = "{{标题}}"
    # 成品图命名：author = 作者.jpg / author_title = 作者_标题.jpg
    # 同名自动加 (2)(3)，不覆盖已有文件
    filename_rule: str = "author_title"
    # 窗口三件套：置顶 / 贴边（left/right/top/空） / 上次窗口位置（-1 表示未记录）
    always_on_top: bool = False
    dock_side: str = ""
    # 拖到屏幕边缘 24px 内松手自动贴边（设置里可关）
    auto_dock: bool = True
    # 关闭主窗 = 隐藏到托盘（防误关）；关掉则关闭即退出
    close_to_tray: bool = True
    win_x: int = -1
    win_y: int = -1
    # 记录窗 / 设置窗各自的位置记忆（-1 表示未记录，打开时居中）
    history_win_x: int = -1
    history_win_y: int = -1
    settings_win_x: int = -1
    settings_win_y: int = -1
    # 界面主题：dark / light，跟随系统不做自动判断，员工在设置里切换
    theme: str = "dark"

    def to_dict(self) -> dict:
        return asdict(self)


def clean_partners(value: object) -> list[str]:
    """规整名单：去空行、去首尾空格、去重（保持原顺序）、限长度。

    设置页是"一行一个名字"的文本框，员工常顺手粘贴出一堆空行和重复项，
    这里统一收口，避免同一台机器上出现"华东出版社"两条。
    """
    if not isinstance(value, (list, tuple)):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in value:
        name = str(raw).strip()[:MAX_NAME_LEN]
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
        if len(cleaned) >= MAX_PARTNERS:
            break
    return cleaned


def template_dir_of(cfg: "ClientConfig") -> Path:
    """配置为空时回落到默认目录，避免空字符串变成当前工作目录"""
    return Path(cfg.template_dir) if cfg.template_dir else DEFAULT_TEMPLATE_DIR
