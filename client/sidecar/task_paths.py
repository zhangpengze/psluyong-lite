"""task_id 的合法形态与安全拼接

task_id 直接拼进文件路径（`<data>/executions/<task_id>/...`），而这个值是**请求
带来的**：`GET /export/status?task_id=`、`POST /export/cancel` 的 body。
`..\\..\\Windows` 这种串进来就能让 sidecar 读写到执行目录之外——单机桌面场景危害
有限（sidecar 只绑 127.0.0.1），但"用户能控制的字符串直接拼路径"从来不该靠运气。

生成侧的形状是 `20261024-101010-abcd1234`（时间戳 + uuid 前 8 位），所以正则
收得很紧：字母数字开头，只允许 `-` 和 `_`，最长 64。收紧的代价是"旧的 task_id
可能不认"——这不成问题，task_id 只存在于 `executions/` 下，reaper 会清掉旧目录。
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_LEN = 64
TASK_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,%d}$" % (MAX_LEN - 1))

# Windows 保留设备名：`CON`、`CON.json`、`nul` 都指设备而不是文件，
# 拿来当目录名会得到"文件已存在"这类看不懂的错
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def is_valid(task_id: object) -> bool:
    """是不是一个能安全拼进路径的 task_id

    不只挡 `..` 和分隔符：盘符、结尾的点/空格、保留设备名都能让路径指到别处
    或直接报错，用白名单正则一次挡完。非 str（None / 数字 / dict）一律 False。
    """
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        return False
    return task_id.split(".")[0].upper() not in _RESERVED


def task_dir(root, task_id: object) -> Path | None:
    """`<root>/executions/<task_id>`；非法 id 返回 None。

    root 由调用方传（而不是在这里 `app_data_dir()`）：测试历来靠
    `monkeypatch.setattr(settler, "app_data_dir", ...)` 把数据目录指到 tmp，
    这里自己取配置的话那个打桩就失效了，用例会往真实目录里写。
    """
    if not is_valid(task_id):
        return None
    return Path(root) / "executions" / str(task_id)


def reject_message() -> str:
    """给接口用的一句人话（前端会原样显示，别写成"参数非法"）"""
    return "task_id 不合法（只允许字母、数字、-、_，最长 64 位）"
