"""report_read：日报列表 / 推送状态 / 正文读取 / 日期反解

这块是「日报页」全部的文件系统逻辑，防目录穿越只此一份实现。
REPORT_DIR 是导入期常量，测试必须把它指到本用例的 tmp——共享 DATA_DIR
里其它用例落的 .md 会让断言随执行顺序漂移（子集顺序敏感的同一个根因）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from server.app.services import report_read


@pytest.fixture()
def report_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "reports"
    d.mkdir()
    monkeypatch.setattr(report_read, "REPORT_DIR", d)
    return d


def _seed(d: Path) -> None:
    (d / "2026-10-21.md").write_text("# 10-21 日报", encoding="utf-8")
    (d / "2026-10-20.md").write_text("# 10-20 日报", encoding="utf-8")
    (d / "2026-10-19.md").write_text("# 10-19 日报", encoding="utf-8")
    (d / "2026-10-21.status.json").write_text(
        json.dumps({"pushed": True, "message": "推送成功"}), encoding="utf-8"
    )


# ---------- 列表 ----------

def test_list_reports_missing_dir_is_empty(tmp_path, monkeypatch):
    """目录还没被创建过（全新部署）：页面要空列表而不是 500"""
    monkeypatch.setattr(report_read, "REPORT_DIR", tmp_path / "从没建过")
    assert report_read.list_reports() == []


def test_list_reports_newest_first_and_only_markdown(report_dir):
    _seed(report_dir)
    (report_dir / "说明.txt").write_text("不该出现在列表里", encoding="utf-8")

    files = report_read.list_reports()

    assert [f.name for f in files] == ["2026-10-21.md", "2026-10-20.md", "2026-10-19.md"]


def test_list_reports_respects_limit(report_dir):
    _seed(report_dir)
    assert [f.name for f in report_read.list_reports(limit=2)] == [
        "2026-10-21.md",
        "2026-10-20.md",
    ]


# ---------- 推送状态 ----------

def test_load_statuses_reads_matching_json(report_dir):
    _seed(report_dir)
    files = report_read.list_reports()

    statuses = report_read.load_statuses(files)

    assert statuses["2026-10-21"]["pushed"] is True
    # 10-20/10-19 没有状态文件：老日报不显示徽标，而不是整个页面报错
    assert "2026-10-20" not in statuses
    assert "2026-10-19" not in statuses


def test_load_statuses_swallows_corrupt_json(report_dir):
    """半截 JSON（断电时原子写没完成的残留）按「无状态」吞掉"""
    (report_dir / "2026-10-21.md").write_text("x", encoding="utf-8")
    (report_dir / "2026-10-21.status.json").write_text('{"pushed": tru', encoding="utf-8")

    assert report_read.load_statuses(report_read.list_reports()) == {}


def test_load_statuses_without_files_is_empty(report_dir):
    assert report_read.load_statuses([]) == {}


# ---------- 正文读取（防穿越） ----------

def test_resolve_content_reads_selected_report(report_dir):
    _seed(report_dir)

    selected, content = report_read.resolve_content("2026-10-21.md", report_read.list_reports())

    assert selected == "2026-10-21.md"
    assert content == "# 10-21 日报"


def test_resolve_content_defaults_to_newest_when_no_selection(report_dir):
    _seed(report_dir)
    files = report_read.list_reports()

    selected, content = report_read.resolve_content("", files)

    assert selected == "2026-10-21.md"
    assert content == "# 10-21 日报"


def test_resolve_content_empty_dir_returns_blank(report_dir):
    assert report_read.resolve_content("", []) == ("", "")


def test_resolve_content_rejects_path_traversal(report_dir, tmp_path):
    """../ 走出 reports/ 的文件必须读不到——安全边界只此一份实现"""
    secret = tmp_path / "机密.md"
    secret.write_text("机密内容", encoding="utf-8")

    selected, content = report_read.resolve_content("../机密.md", [])

    assert selected == "../机密.md"  # selected 原样返回，页面照常渲染
    assert content == ""


def test_resolve_content_rejects_non_markdown(report_dir):
    (report_dir / "2026-10-21.status.json").write_text('{"a":1}', encoding="utf-8")

    assert report_read.resolve_content("2026-10-21.status.json", []) == (
        "2026-10-21.status.json",
        "",
    )


def test_resolve_content_missing_file_returns_blank(report_dir):
    assert report_read.resolve_content("2099-01-01.md", []) == ("2099-01-01.md", "")


# ---------- 日期反解 ----------

def test_parse_report_date_from_file_name():
    assert report_read.parse_report_date("2026-10-21.md") == date(2026, 10, 21)


def test_parse_report_date_unparseable_is_none():
    """反解不出来只能是 None：让接口把「今天」硬推给全公司更糟"""
    assert report_read.parse_report_date("") is None
    assert report_read.parse_report_date("随手写的日报.md") is None
