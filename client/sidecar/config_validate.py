"""客户端配置的写入校验与读取纠偏

两个入口：
- `sanitize_config_patch`：HTTP 提交的补丁 → 可落盘的值（坏值整包拒绝）
- `_coerce_config`：磁盘上读到的 JSON → 可用的值（坏字段单独纠正，不牵连全文）

后者是"员工手改了 config.json / 旧版本留下的脏数据"这一侧的保险：
一个字段类型不对不能让所有接口 500。
"""

from __future__ import annotations

import re

from .config_schema import ClientConfig, clean_partners


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


def _normalize_path(value: object) -> str:
    """折叠重复分隔符。

    用户在设置页粘贴路径时常带 `C://Program Files//...` 这种双斜杠，
    Windows 能忍但日志和界面上很难看。注意不能碰 UNC 路径（\\\\server\\share）。
    """
    if not isinstance(value, str):
        # 手工改坏的 config.json 里可能是数字/数组：这里不能炸，
        # 一炸就是所有接口 500（员工在界面上修不回来，只能手删文件）
        return ""
    if not value or value.startswith("\\\\") or value.startswith("//"):
        return value
    sep = "\\" if "\\" in value else "/"
    # 替换串用 lambda：直接传 "\\" 会被 re 当成转义模板，报 bad escape
    return re.sub(r"[\\/]{2,}", lambda _m: sep, value)


def _coerce_config(raw: dict) -> tuple[dict, list[str]]:
    """逐字段纠正类型：一个字段坏了只影响它自己，不牵连整份配置。

    旧写法 `ClientConfig(**raw)` 完全没有校验——`{"output_dir": 123}` 一路
    走到 `_normalize_path` 才 `AttributeError`，结果是每个接口 500，
    员工在界面上根本修不了。这里坏字段一律回默认值并记一条警告。
    """
    defaults = ClientConfig()
    clean: dict = {}
    problems: list[str] = []

    for key in ClientConfig.__dataclass_fields__:
        if key not in raw:
            continue
        value = raw[key]

        if key == "partners":
            if isinstance(value, list):
                clean[key] = value
            else:
                problems.append("partners 不是数组，已用空名单")
        elif key in _BOOLS:
            if isinstance(value, bool):
                clean[key] = value
            else:
                problems.append(f"{key} 不是布尔值（{value!r}），已用默认值")
        elif key in _INT_RANGES:
            low, high = _INT_RANGES[key]
            num = _as_int(value)
            if num is None:
                problems.append(f"{key} 不是整数（{value!r}），已用默认值")
            elif not low <= num <= high:
                clean[key] = min(max(num, low), high)
                problems.append(f"{key}={num} 超出 {low}~{high}，已纠正为 {clean[key]}")
            else:
                clean[key] = num
        elif key in _INT_ANY:
            num = _as_int(value)
            if num is None:
                problems.append(f"{key} 不是整数（{value!r}），已用默认值")
            else:
                clean[key] = num
        elif key in _ENUMS:
            allowed = _ENUMS[key]
            if isinstance(value, str) and value in allowed:
                clean[key] = value
            else:
                problems.append(f"{key} 取值不合法（{value!r}），已用默认值 {getattr(defaults, key)!r}")
        elif key in _STR_LIMITS:
            if isinstance(value, str):
                # 去掉首尾空白：API Key 后面多一个空格是最经典的"Key 明明对却 401"
                clean[key] = value.strip()[: _STR_LIMITS[key]]
            else:
                problems.append(f"{key} 不是字符串（{value!r}），已用默认值")
        elif isinstance(value, str):
            clean[key] = value
        else:
            problems.append(f"{key} 类型异常（{value!r}），已用默认值")
    return clean, problems
