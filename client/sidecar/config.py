"""客户端本地配置

存在 %APPDATA%/psluyong-lite/config.json（开发时可被环境变量覆盖）。
不写注册表、不写安装目录——安装目录可能没有写权限。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


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
    # 合作伙伴名单：员工自己维护，存本机，断网也能选
    partners: list[str] = field(default_factory=list)
    # PSD 模板所在目录（员工把模板文件拷进去即可）
    template_dir: str = field(default_factory=lambda: str(DEFAULT_TEMPLATE_DIR))
    # 模板里两组待替换的查找文字（全局固定，和原项目一致，在设置页修改）
    author_placeholder: str = "{{作者}}"
    title_placeholder: str = "{{标题}}"
    # 窗口三件套：置顶 / 贴边（left/right/空） / 上次窗口位置（-1 表示未记录）
    always_on_top: bool = False
    dock_side: str = ""
    win_x: int = -1
    win_y: int = -1

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


def _normalize_path(value: str) -> str:
    """折叠重复分隔符。

    用户在设置页粘贴路径时常带 `C://Program Files//...` 这种双斜杠，
    Windows 能忍但日志和界面上很难看。注意不能碰 UNC 路径（\\\\server\\share）。
    """
    import re

    if not value or value.startswith("\\\\") or value.startswith("//"):
        return value
    sep = "\\" if "\\" in value else "/"
    # 替换串用 lambda：直接传 "\\" 会被 re 当成转义模板，报 bad escape
    return re.sub(r"[\\/]{2,}", lambda _m: sep, value)


def load_config() -> ClientConfig:
    if not CONFIG_PATH.exists():
        cfg = ClientConfig()
        save_config(cfg)
        return cfg
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.error("配置文件损坏，回退默认值: %s", e)
        return ClientConfig()

    known = {f for f in ClientConfig.__dataclass_fields__}
    cfg = ClientConfig(**{k: v for k, v in raw.items() if k in known})
    cfg.ps_path = _normalize_path(cfg.ps_path)
    cfg.output_dir = _normalize_path(cfg.output_dir)
    cfg.template_dir = _normalize_path(cfg.template_dir)
    cfg.partners = clean_partners(cfg.partners)
    return cfg


def save_config(cfg: ClientConfig) -> None:
    cfg.ps_path = _normalize_path(cfg.ps_path)
    cfg.output_dir = _normalize_path(cfg.output_dir)
    cfg.template_dir = _normalize_path(cfg.template_dir)
    cfg.partners = clean_partners(cfg.partners)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ensure_dirs() -> None:
    for path in (DATA_DIR, CACHE_DIR, TEMPLATE_CACHE, QUEUE_DIR, DEFAULT_TEMPLATE_DIR):
        path.mkdir(parents=True, exist_ok=True)
