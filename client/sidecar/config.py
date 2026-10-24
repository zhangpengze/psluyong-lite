"""客户端本地配置的落盘：原子写 + 备份 + 自愈

存在 %APPDATA%/psluyong-lite/config.json（开发时可被环境变量覆盖）。
不写注册表、不写安装目录——安装目录可能没有写权限。

这个文件是员工机器上**唯一**的配置来源，写坏等于客户端报废：没有自动更新，
也就没人能远程把它改回来（只能逐台远程）。所以三道保险：
  1. 写入一律 tmp + os.replace —— 读方只会看到旧文件或新文件，看不到半截
  2. 每次保存前留一份 config.json.bak（只有当前文件能正常解析时才刷新）
  3. 读到坏文件不静默吞掉：改名 config.broken-<时间>.json 留档 → 试 .bak 恢复
     → 实在没有可用备份才回默认值，并把这事报给界面
字段定义与校验在 config_schema.py，这里只管"怎么安全地读写"。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

# 这些名字历史上都由本模块提供，其它模块（以及测试）从这里导入
from .config_schema import (  # noqa: F401
    CACHE_DIR,
    CONFIG_PATH,
    DATA_DIR,
    DEFAULT_TEMPLATE_DIR,
    QUEUE_DIR,
    TEMPLATE_CACHE,
    ClientConfig,
    app_data_dir,
    clean_partners,
    template_dir_of,
)
from .config_validate import _coerce_config, _normalize_path, sanitize_config_patch  # noqa: F401

logger = logging.getLogger(__name__)


class ConfigBusyError(RuntimeError):
    """配置文件暂时读不到（被占用），为避免把默认值写回去，本次拒绝写入。"""


# 进程内锁：/config 保存、窗口位置记忆、主题切换、/status 读，都可能并发
_SAVE_LOCK = threading.RLock()
# 上一次自愈动作的说明（空 = 没出过事）。sidecar 进程内有效，重启即清空。
_LAST_RECOVERY = ""

# Windows 上 os.replace 撞上"另一线程正打开着目标文件读"会报
# PermissionError(32 共享冲突)——文件只有几 KB，读方微秒级就放开了，
# 重试几次即可。总等待 ~0.3s，比让员工看到一次保存失败划算得多。
_REPLACE_RETRIES = 6
# 读的重试次数：并发替换文件时读也会短时拿不到（同一种共享冲突）
_READ_RETRIES = 4


def _bak_path(path: Path | None = None) -> Path:
    target = path or CONFIG_PATH
    return target.with_name(target.name + ".bak")


def _replace_with_retry(tmp: Path, path: Path) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def _write_text_atomic(path: Path, text: str) -> None:
    """先写同目录临时文件再 os.replace：任何时刻磁盘上都是一个完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp 保证临时文件名唯一：并发写不会互相踩同一个 .tmp
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())  # 断电时不至于留下一个 0 字节的新文件
        _replace_with_retry(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_text_with_retry(path: Path) -> tuple[str | None, str, bool]:
    """返回 (文本, 问题说明, 是否"暂时读不到")。

    第三项是关键：Windows 上另一线程正 `os.replace` 顶替这个文件时，读会拿到
    `PermissionError`（共享冲突）。那是**瞬时**的，不能当"文件损坏"处理——
    否则一次并发读就会把好文件改名留档，然后回默认值把员工的 Key 冲掉。
    """
    last = ""
    for attempt in range(_READ_RETRIES):
        try:
            return path.read_text(encoding="utf-8"), "", False
        except FileNotFoundError:
            return None, "", False
        except OSError as e:  # PermissionError / 被杀软、备份软件占着
            last = str(e)
            if attempt < _READ_RETRIES - 1:
                time.sleep(0.02 * (attempt + 1))
    return None, f"读不出来（{last}）", True


def _parse_config_file(path: Path) -> tuple[dict | None, str, bool]:
    """返回 (内容, 问题说明, 是否暂时读不到)。内容为 None 说明没拿到可用的配置。"""
    text, problem, transient = _read_text_with_retry(path)
    if text is None:
        return None, problem, transient
    if not text.strip():
        return None, "文件是空的", False
    try:
        raw = json.loads(text)
    except ValueError as e:
        return None, f"不是合法 JSON（{e}）", False
    if not isinstance(raw, dict):
        return None, f"顶层不是 JSON 对象（是 {type(raw).__name__}）", False
    return raw, "", False


def _build_config(raw: dict) -> ClientConfig:
    values, problems = _coerce_config(raw)
    if problems:
        logger.warning("config.json 有 %d 项不合法，已纠正：%s", len(problems), "；".join(problems))
    cfg = ClientConfig(**values)
    cfg.ps_path = _normalize_path(cfg.ps_path)
    cfg.output_dir = _normalize_path(cfg.output_dir)
    cfg.template_dir = _normalize_path(cfg.template_dir)
    cfg.partners = clean_partners(cfg.partners)
    return cfg


def _refresh_backup_locked() -> None:
    """把当前文件复制成 .bak；当前文件解析不了就**不动**备份。

    坏文件状态下继续保存（比如员工在设置里改了一项）时，如果无脑刷新备份，
    好的 .bak 就被坏的覆盖了，之后再坏就彻底没得救。
    """
    text, problem, _transient = _read_text_with_retry(CONFIG_PATH)
    if text is None:
        if problem:
            logger.warning("当前配置读不出来（%s），本次不刷新备份，保留旧 .bak", problem)
        return
    try:
        json.loads(text)
    except ValueError:
        logger.warning("当前配置无法解析，本次不刷新备份，保留旧 .bak")
        return
    try:
        _write_text_atomic(_bak_path(), text)
    except OSError as e:
        # 备份失败不能阻断保存：主文件写入才是这次操作的目的
        logger.warning("配置备份写入失败（不影响本次保存）：%s", e)


def _save_locked(cfg: ClientConfig) -> None:
    cfg.ps_path = _normalize_path(cfg.ps_path)
    cfg.output_dir = _normalize_path(cfg.output_dir)
    cfg.template_dir = _normalize_path(cfg.template_dir)
    cfg.partners = clean_partners(cfg.partners)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _refresh_backup_locked()
    _write_text_atomic(CONFIG_PATH, json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))


def _quarantine_locked(problem: str) -> None:
    """坏文件改名留档：别删——里面有员工真实填过的服务器地址和 Key 片段，
    报障时能捞回来；文件名带时间戳，员工反复出问题也能看出规律。"""
    global _LAST_RECOVERY
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = CONFIG_PATH.with_name(f"config.broken-{stamp}.json")
    try:
        _replace_with_retry(CONFIG_PATH, target)
        logger.error("配置文件损坏（%s），已留档为 %s", problem, target.name)
        _LAST_RECOVERY = f"配置文件损坏（{problem}），原文件已留档为 {target.name}"
    except OSError as e:
        logger.error("配置文件损坏（%s），且无法留档：%s", problem, e)
        _LAST_RECOVERY = f"配置文件损坏（{problem}）"


def _load_locked_state() -> tuple[ClientConfig, str]:
    """返回 (配置, 不可写原因)。原因非空 = 盘上内容不明，**绝不能回写**。"""
    global _LAST_RECOVERY
    raw, problem, transient = _parse_config_file(CONFIG_PATH)
    if raw is not None:
        return _build_config(raw), ""

    if transient:
        # 文件本身没坏，只是这一瞬间读不到（杀软/备份软件/别的线程正在换名）。
        # 回默认值写盘才是真正的配置丢失，所以只撑过这一次请求。
        logger.warning("配置暂时读不出来：%s", problem)
        return ClientConfig(), f"配置文件暂时被占用（{problem}），为避免覆盖你的设置，本次不写入，请稍后重试"

    if problem:
        _quarantine_locked(problem)

    bak_raw, bak_problem, bak_transient = _parse_config_file(_bak_path())
    if bak_raw is not None:
        cfg = _build_config(bak_raw)
        _save_locked(cfg)
        logger.warning("已从备份恢复配置（服务器 %s / 员工 %s）", cfg.server_url, cfg.employee_name or "未设置")
        _LAST_RECOVERY = f"{_LAST_RECOVERY}；已从备份 config.json.bak 恢复".lstrip("；")
        return cfg, ""

    if bak_transient:
        # 主文件和备份同时读不到：盘上情况不明，不写
        return ClientConfig(), f"配置和备份都暂时读不出来（{bak_problem}），本次不写入，请稍后重试"

    cfg = ClientConfig()
    if problem:
        # 有坏文件但没可用备份：只能用默认值，但把话说清楚，别让员工
        # 以为"软件还记得我的 Key"
        _LAST_RECOVERY = f"{_LAST_RECOVERY}；没有可用备份，已重置为默认值，请在设置里重填服务器地址和 API Key"
    if bak_problem:
        logger.warning("备份同样不可用（%s）", bak_problem)
    _save_locked(cfg)
    return cfg, ""


def load_config() -> ClientConfig:
    with _SAVE_LOCK:
        return _load_locked_state()[0]


def save_config(cfg: ClientConfig) -> None:
    with _SAVE_LOCK:
        _save_locked(cfg)


def update_config(patch: dict) -> ClientConfig:
    """加锁的"读-改-写"：两个请求同时保存时不会互相覆盖。

    旧写法在路由里先 load 再 setattr 再 save，中间没有任何互斥——窗口位置
    记忆（每个窗口 10s 一次）和设置页保存撞上时，后写的那个会带着旧快照
    把对方刚改的字段盖回去。
    """
    with _SAVE_LOCK:
        cfg, blocked = _load_locked_state()
        if blocked:
            # 读不到当前内容还硬写 = 拿默认值把员工的 Key 冲掉
            raise ConfigBusyError(blocked)
        for key, value in patch.items():
            setattr(cfg, key, value)
        _save_locked(cfg)
        return cfg


def last_recovery() -> str:
    """上次自愈的说明（给 /status 用）。空字符串 = 本进程没出过事。"""
    return _LAST_RECOVERY


def reset_last_recovery() -> None:
    """测试用：清掉进程级状态，避免用例之间互相干扰。"""
    global _LAST_RECOVERY
    _LAST_RECOVERY = ""


def ensure_dirs() -> None:
    for path in (DATA_DIR, CACHE_DIR, TEMPLATE_CACHE, QUEUE_DIR, DEFAULT_TEMPLATE_DIR):
        path.mkdir(parents=True, exist_ok=True)
