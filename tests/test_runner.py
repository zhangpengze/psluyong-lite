"""成品图命名规则测试

命名直接决定员工能否在输出目录里一眼找到某人的图，且一旦覆盖就丢成品，
所以规则与去重都要有测试兜着（不需要真 PS，纯路径计算）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.runner import (
    DEFAULT_FILENAME_RULE,
    FILENAME_RULE_AUTHOR,
    FILENAME_RULE_AUTHOR_TITLE,
    ExportRequest,
    build_output_path,
)


def _req(tmp_path: Path, rule: str | None = None, author: str = "李四", title: str = "基于深度学习的图像分割") -> ExportRequest:
    kwargs = {} if rule is None else {"filename_rule": rule}
    return ExportRequest(
        template_path=tmp_path / "t.psd",
        author=author,
        title=title,
        output_dir=tmp_path,
        **kwargs,
    )


def test_author_rule_uses_author_only(tmp_path):
    assert build_output_path(_req(tmp_path, FILENAME_RULE_AUTHOR)).name == "李四.jpg"


def test_author_title_rule_joins_with_underscore(tmp_path):
    assert build_output_path(_req(tmp_path, FILENAME_RULE_AUTHOR_TITLE)).name == "李四_基于深度学习的图像分割.jpg"


def test_default_rule_is_author_title(tmp_path):
    assert DEFAULT_FILENAME_RULE == FILENAME_RULE_AUTHOR_TITLE
    assert build_output_path(_req(tmp_path)).name == "李四_基于深度学习的图像分割.jpg"


def test_unknown_rule_falls_back_to_default(tmp_path):
    """脏配置不能把文件名搞成空的"""
    assert build_output_path(_req(tmp_path, "随便写的")).name == "李四_基于深度学习的图像分割.jpg"


@pytest.mark.parametrize("rule", [FILENAME_RULE_AUTHOR, FILENAME_RULE_AUTHOR_TITLE])
def test_existing_file_is_never_overwritten(tmp_path, rule):
    """同名不覆盖：同一个人第二次生成不能把上一张顶掉"""
    first = build_output_path(_req(tmp_path, rule))
    first.write_bytes(b"first")

    second = build_output_path(_req(tmp_path, rule))
    assert second != first
    assert second.name.endswith("(2).jpg")
    second.write_bytes(b"second")

    third = build_output_path(_req(tmp_path, rule))
    assert third.name.endswith("(3).jpg")
    assert first.read_bytes() == b"first"


def test_illegal_chars_are_sanitized(tmp_path):
    name = build_output_path(_req(tmp_path, FILENAME_RULE_AUTHOR_TITLE, author='李/四:*?', title='a"b<c>')).name
    assert name == "李_四_a_b_c.jpg"


def test_no_timestamp_in_filename(tmp_path):
    """员工反馈过：作者_标题_20261021_183343 这种名字在文件夹里根本找不到"""
    import re

    name = build_output_path(_req(tmp_path, FILENAME_RULE_AUTHOR_TITLE)).name
    assert not re.search(r"\d{8}[_-]\d{6}", name)


def test_empty_title_does_not_leave_dangling_separator(tmp_path):
    """作者+标题规则下标题空白（理论上会被上游拦住，这里兜底）"""
    assert build_output_path(_req(tmp_path, FILENAME_RULE_AUTHOR_TITLE, title="   ")).name == "李四_未命名.jpg"
