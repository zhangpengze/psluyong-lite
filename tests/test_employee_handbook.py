"""docs/员工手册.md 的"不许过时"检查。

手册是给员工看的。写错一个按钮名，员工就会在界面上找一个不存在的东西——
这比没有手册更糟，因为他会以为是自己看错了。而它最容易过时的原因恰恰是
"改了界面没人想起改文档"，所以这里把手册里"指向具体界面"的那几句，
逐条对着真实源码校一遍。

这些用例真的抓到过问题：写手册时把主按钮记成「生成」，实际是「生成录用图」；
冷启动失败的提示语里也写错成「生成图片」——两处都是靠下面第 3、5 条揪出来的。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HANDBOOK = REPO / "docs" / "员工手册.md"
RECORD_FORM = REPO / "client" / "src" / "components" / "RecordForm.vue"
SETTINGS_PANEL = REPO / "client" / "src" / "components" / "SettingsPanel.vue"
ADVANCED_TAB = REPO / "client" / "src" / "components" / "settings" / "AdvancedTab.vue"
APP_HEADER = REPO / "client" / "src" / "components" / "AppHeader.vue"
EXPORT_TASK = REPO / "client" / "src" / "composables" / "useExportTask.ts"
CONNECTOR = REPO / "core" / "ps_connector.py"


def _text(path: Path) -> str:
    assert path.exists(), f"找不到 {path.relative_to(REPO)}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doc() -> str:
    return _text(HANDBOOK)


# ---------- 一页纸 ----------

def test_handbook_is_actually_one_page(doc):
    """说好"一页纸"就得真是一页：超过这个长度员工不会读完，写了等于没写"""
    lines = [ln for ln in doc.splitlines() if ln.strip()]
    assert len(lines) <= 110, f"手册已经 {len(lines)} 行，不再是一页纸了"


def test_handbook_starts_with_the_real_cause(doc):
    """九成故障是 PS 没开，这条必须排在最前面，不能埋在后面"""
    first_section = doc.split("## ", 2)[1]
    assert "Photoshop" in first_section


# ---------- 按钮名必须和界面一致 ----------

def test_primary_button_name_matches_ui(doc):
    ui = _text(RECORD_FORM)
    for name in ("生成录用图", "取消生成"):
        assert name in ui, f"界面里没有「{name}」了，手册要跟着改"
        assert name in doc, f"手册没提「{name}」，员工会找不到这个按钮"


def test_handbook_never_calls_it_the_old_name(doc):
    """旧名「生成图片」曾经同时出现在手册和报错文案里，界面上根本没有这个东西"""
    assert "生成图片" not in doc


def test_diagnostics_button_name_matches_ui(doc):
    assert "导出诊断包" in _text(ADVANCED_TAB)
    assert "导出诊断包" in doc


def test_error_messages_name_a_button_that_exists():
    """报错文案里让员工"再点一次「XXX」"时，XXX 必须是界面上真有的按钮。

    冷启动超时那句曾经写的是「生成图片」——员工照着找，界面上只有「生成录用图」。
    这类错字不影响程序运行，所以永远不会被别的用例发现，只能这样校。
    """
    connector = _text(CONNECTOR)
    record_form = _text(RECORD_FORM)
    quoted = {m for m in re.findall(r"「([^」]+)」", connector) if "生成" in m or "取消" in m}
    assert quoted, "连接失败的提示里应该点名让员工点哪个按钮"
    for name in quoted:
        assert name in record_form, f"提示语让员工点「{name}」，但界面里没有这个按钮"


# ---------- 手册引用的界面文案 ----------

def test_offline_banner_wording_matches_ui(doc):
    """手册让员工"看到红点不用管"，那它就得引用界面上真实的那句话"""
    ui = _text(APP_HEADER)
    assert "服务器离线，记录会存在本地" in ui
    assert "服务器离线，记录会存在本地" in doc


def test_progress_phrases_are_all_mentioned(doc):
    """手册说"看状态行卡在哪一步"，那四句阶段文案必须一句不差地列出来"""
    phases = re.findall(r"return '([^']+)'", _text(EXPORT_TASK))
    phases = [p for p in phases if p.startswith("正在")]
    assert len(phases) >= 3, phases
    for phase in phases:
        assert phase in doc, f"手册没写阶段文案「{phase}」"


def test_cancel_threshold_matches_code(doc):
    match = re.search(r"CANCEL_AFTER_S\s*=\s*(\d+)", _text(EXPORT_TASK))
    assert match, "找不到 CANCEL_AFTER_S"
    assert f"{match.group(1)} 秒" in doc


def test_cold_start_budget_in_handbook_matches_code(doc):
    from core import ps_retry

    assert int(ps_retry.COLD_START_BUDGET_S) == 60
    assert "60 秒" in doc


def test_template_missing_wording_matches_code(doc):
    """手册让员工照着屏幕上的话判断该怎么处理，文案得对得上"""
    combined = _text(CONNECTOR) + _text(REPO / "client" / "sidecar" / "file_ops.py")
    assert "模板文件找不到了" in combined
    assert "模板文件找不到了" in doc


def test_placeholder_error_wording_matches_code(doc):
    from core import ps_rules

    assert "模板里找不到占位符" in ps_rules.required_miss_message(["a"])
    assert "模板里找不到占位符" in doc


# ---------- 设置页的分组名 ----------

def test_settings_tab_names_match_ui(doc):
    ui = _text(SETTINGS_PANEL)
    for name in ("账号", "模板", "生成", "窗口", "高级"):
        assert f"label: '{name}'" in ui, f"设置页没有「{name}」这个分组了"


def test_handbook_paths_for_settings_are_real(doc):
    """手册写"设置 → 高级 → 输出目录"，这三步界面上都得真能走通"""
    assert "设置 → 高级" in doc or "设置（右上齿轮）→ **高级**" in doc
    assert "输出目录" in _text(ADVANCED_TAB)


# ---------- 默认输出目录 ----------

def test_default_output_dir_claim_is_true(doc):
    """曾经的错误：把占位符示例 D:\\期刊录用 当成了默认目录"""
    from client.sidecar.config_schema import ClientConfig

    default = ClientConfig().output_dir
    assert "期刊录用" in default
    assert "文档" in doc and "期刊录用" in doc
    assert "D:\\期刊录用" not in doc.split("设置 → 高级")[0]


# ---------- 报障路径 ----------

def test_handbook_tells_employee_to_attach_diagnostics(doc):
    """没让他附诊断包，主管就得来回问三轮"""
    assert "诊断包" in doc
    assert "发给主管" in doc or "发给" in doc


def test_handbook_warns_not_to_borrow_api_key(doc):
    """一人一个 Key 是后台统计工作量的前提，借用了工作量就记错人"""
    assert "密钥" in doc
    assert "借" in doc
