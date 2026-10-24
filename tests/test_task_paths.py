"""task_id 的形态校验（client/sidecar/task_paths.py）

task_id 来自请求（轮询 query、取消 body），却直接拼进文件路径。`..\\..\\Windows`
这种串进来就能让 sidecar 读写执行目录之外——虽然 sidecar 只绑 127.0.0.1、危害有限，
但"用户能控制的字符串直接拼路径"不该靠运气。

这里钉两件事：**生成侧的形状必须被接受**（别把正常请求拦掉），**任何能跳出
executions/ 的形状必须被拒**（含 Windows 特有的保留设备名与盘符）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from client.sidecar import task_paths

# executor.start 真实生成的形状：时间戳 + uuid 前 8 位
REAL_TASK_ID = "20261024-101010-abcd1234"


@pytest.mark.parametrize(
    "task_id",
    [
        REAL_TASK_ID,
        "abc",
        "A1",
        "with_underscore-and-dash",
        "x" * task_paths.MAX_LEN,
        "20261024-101010-abcd1234",
    ],
)
def test_accepts_what_we_generate(task_id):
    assert task_paths.is_valid(task_id) is True


@pytest.mark.parametrize(
    "task_id",
    [
        "",  # 空
        "..",  # 父目录
        "../x",  # 纵向穿越（POSIX 风格）
        "..\\x",  # 纵向穿越（Windows 风格）
        "a/b",  # 分隔符
        "a\\b",
        "C:\\Windows",  # 盘符
        "/etc/passwd",  # 绝对路径
        "a b",  # 空格
        "a.b",  # 点（真实 id 不含点）
        "a$b",  # 通配符/变量字符
        "-leading-dash",  # 结尾点是 Windows 的特殊处理，开头的横线同理别放行
        "x" * (task_paths.MAX_LEN + 1),  # 超长
        None,
        123,
        {"task_id": "x"},
    ],
)
def test_rejects_everything_that_could_escape(task_id):
    assert task_paths.is_valid(task_id) is False


@pytest.mark.parametrize("name", ["CON", "con", "NUL", "nul.json", "COM1", "lpt9"])
def test_rejects_windows_reserved_device_names(name):
    """`mkdir CON` 在 Windows 上会失败并给出看不懂的错，或更好彩地指到设备上"""
    assert task_paths.is_valid(name) is False


def test_task_dir_stays_inside_executions(tmp_path):
    assert task_paths.task_dir(tmp_path, REAL_TASK_ID) == tmp_path / "executions" / REAL_TASK_ID


@pytest.mark.parametrize("task_id", ["../x", "..\\x", "C:\\Windows", "", None])
def test_task_dir_refuses_to_build_an_escaping_path(tmp_path, task_id):
    assert task_paths.task_dir(tmp_path, task_id) is None


def test_task_dir_never_writes(tmp_path):
    """只拼路径，不建目录：拼错的时候不该顺手在盘上留下东西"""
    task_paths.task_dir(tmp_path, REAL_TASK_ID)
    task_paths.task_dir(tmp_path, "../x")

    assert list(Path(tmp_path).iterdir()) == []
