"""core/ps_rules：替换规则的命中覆盖判定（纯逻辑，边界全在这里）。"""

from __future__ import annotations

import pytest

from core.ps_rules import (
    optional_missing_warnings,
    required_miss_message,
    required_missing,
)


def _rule(find: str, **kw):
    return {"find": find, "replace": "X", "regex": False, "match_mode": "包含", **kw}


def test_required_is_default_so_miss_fails():
    assert required_missing([_rule("{{a}}"), _rule("{{b}}")], matched={0}) == ["{{b}}"]


def test_matched_rules_are_not_missing():
    assert required_missing([_rule("{{a}}"), _rule("{{b}}")], matched={0, 1}) == []


def test_explicit_optional_excluded_from_required():
    rules = [_rule("{{a}}"), _rule("{{b}}", required=False)]
    assert required_missing(rules, matched=set()) == ["{{a}}"]
    assert optional_missing_warnings(rules, matched=set()) == ["第 2 条替换规则未命中，请核对成品"]


def test_empty_find_never_counts():
    """空 find 是脏配置：跳过，既不判失败也不报警告，否则员工会去翻模板。"""
    rules = [_rule(""), _rule("{{a}}")]
    assert required_missing(rules, matched={1}) == []
    assert optional_missing_warnings(rules, matched={1}) == []


def test_skip_excludes_rules_already_reported_elsewhere():
    """正则写错的规则归"正则规则无效"管，不该被说成模板缺占位符。"""
    rules = [_rule("[unclosed", regex=True)]
    assert required_missing(rules, matched=set()) == ["[unclosed"]
    assert required_missing(rules, matched=set(), skip={0}) == []


def test_required_false_string_is_truthy_trap():
    """配置来自 JSON/表单，字符串 "false" 是真值 —— 显式写清楚，别让人踩。"""
    rules = [_rule("{{a}}", required="false")]
    assert required_missing(rules, matched=set()) == ["{{a}}"], "字符串 false 按真值处理（已有行为）"


def test_message_names_every_missing_placeholder():
    msg = required_miss_message(["{{标题}}", "{{刊期}}"])
    assert "「{{标题}}」" in msg and "「{{刊期}}」" in msg
    assert "逐字核对" in msg, "必须给出下一步动作"


@pytest.mark.parametrize("missing", [["{{a}}"], ["{{a}}", "{{b}}"]])
def test_message_is_never_empty(missing):
    assert required_miss_message(missing).strip()
