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


# ========== 写入校验 ==========
# 设置页的滑块和下拉框自带范围，但 HTTP 接口没有。直接 POST 一个
# jpg_quality=999 就能把 PS 导出参数搞坏，而且这个值会被写进 config.json，
# 下次启动继续生效，从界面上完全看不出来哪里错了。所以落盘前统一收口。

_INT_RANGES: dict[str, tuple[int, int]] = {
    "jpg_quality": (1, 12),
}
# 六个数都是窗口坐标：可以是任意整数，-1 表示"未记录"
_INT_ANY: set[str] = {
    "win_x",
    "win_y",
    "history_win_x",
    "history_win_y",
    "settings_win_x",
    "settings_win_y",
}
_STR_LIMITS: dict[str, int] = {
    "server_url": 300,
    "api_key": 256,
    "employee_name": 100,
    "ps_path": 512,
    "output_dir": 512,
    "template_dir": 512,
    "author_placeholder": 64,
    "title_placeholder": 64,
    "last_template_id": 255,
    "last_partner": 100,
    "last_author": 200,
}
_ENUMS: dict[str, set[str]] = {
    "theme": {"dark", "light"},
    "filename_rule": {"author", "author_title"},
    "dock_side": {"", "left", "right", "top"},
}
_BOOLS: set[str] = {"auto_clear", "always_on_top", "auto_dock", "close_to_tray"}


def _as_int(value: object) -> int | None:
    """宽松取整：JSON 里 8.0 和 "8" 都当 8，其余一律 None。

    bool 必须先挡在前面——Python 里 True 是 int 的子类，不挡的话
    `jpg_quality=true` 会被静默当成 1。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            try:
                return int(text)
            except ValueError:
                return None
    return None


def sanitize_config_patch(payload: object) -> tuple[dict, list[str]]:
    """把客户端提交的配置补丁规整成可安全落盘的形式。

    返回 (clean, errors)。**errors 非空时调用方应整包拒绝**：部分落盘会让
    配置处在"一半新值一半旧值"的状态，比直接报错难排查得多。

    未知字段一律报错而不是静默忽略——拼错一个 key 却"保存成功"，
    员工会以为自己改了设置。
    """
    if not isinstance(payload, dict):
        return {}, ["配置必须是 JSON 对象"]

    known = set(ClientConfig.__dataclass_fields__)
    clean: dict = {}
    errors: list[str] = []

    for key, value in payload.items():
        if key not in known:
            errors.append(f"未知配置项 {key}")
            continue
        if value is None:
            continue  # 前端偶尔传 null，语义是"这一项不改"

        if key in _BOOLS:
            if not isinstance(value, bool):
                errors.append(f"{key} 必须是布尔值")
            else:
                clean[key] = value

        elif key in _INT_RANGES:
            low, high = _INT_RANGES[key]
            num = _as_int(value)
            if num is None:
                errors.append(f"{key} 必须是整数")
            elif not low <= num <= high:
                errors.append(f"{key} 必须在 {low}~{high} 之间")
            else:
                clean[key] = num

        elif key in _INT_ANY:
            num = _as_int(value)
            if num is None:
                errors.append(f"{key} 必须是整数")
            else:
                clean[key] = num

        elif key == "partners":
            if not isinstance(value, list):
                errors.append("partners 必须是字符串数组")
            else:
                clean[key] = clean_partners(value)

        elif key in _ENUMS:
            allowed = _ENUMS[key]
            if not isinstance(value, str) or value not in allowed:
                names = "、".join(sorted(v for v in allowed if v)) or "空"
                errors.append(f"{key} 只能是 {names}")
            else:
                clean[key] = value

        elif key in _STR_LIMITS:
            if not isinstance(value, str):
                errors.append(f"{key} 必须是字符串")
            else:
                clean[key] = value.strip()[: _STR_LIMITS[key]]

        else:
            # 新增了 dataclass 字段却忘了在这里登记：宁可报错，
            # 也不要让它悄悄写进 config.json
            errors.append(f"{key} 暂不支持修改")

    return clean, errors


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
