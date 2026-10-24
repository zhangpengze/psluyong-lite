"""仪表盘的导出渲染：HTML 片段 + CSV。

为什么由服务端渲染、不让 JS 拼 DOM：
    页面首屏要服务端直出（无 JS / 弱网也能看），轮询更新又要同一套标记。
    两套渲染必然漂移，所以只保留一套——Python 生成片段，Jinja 用 |safe
    输出首屏，dashboard.js 轮询时 innerHTML 原地替换。
    用户输入（伙伴名 / 模板名）全部 html.escape，片段里不存在未转义数据，
    innerHTML 不会成为 XSS 入口。

按子功能拆分（单文件 ≤300 行门禁）：
    charts.py      KPI 卡 / 趋势 SVG / TOP 5 条形
    partner_table.py 伙伴明细表 / 行展开明细 / 口径注脚
    status_card.py 系统状态卡
    export_csv.py  概览 CSV 导出
    _format.py     共用格式化小函数
本文件只留聚合入口 overview_html 与公开名字的兼容 re-export，import 方不用改。
"""

from __future__ import annotations

from .charts import kpi_html, top_html, trend_html
from .export_csv import overview_csv
from .partner_table import detail_html, note_html, table_html
from .status_card import status_html

__all__ = [
    "detail_html",
    "kpi_html",
    "note_html",
    "overview_csv",
    "overview_html",
    "status_html",
    "table_html",
    "top_html",
    "trend_html",
]


def overview_html(ov: dict, limit: int = 20) -> dict:
    """一次拿齐所有片段；details 与 partners 数组按序对齐（不受 limit 截断影响）。"""
    return {
        "kpi": kpi_html(ov),
        "trend": trend_html(ov),
        "top": top_html(ov),
        "table": table_html(ov, limit),
        "details": [detail_html(ov, p) for p in ov["partners"]],
        "note": note_html(ov, limit),
        "status": status_html(ov["system"]),
    }
