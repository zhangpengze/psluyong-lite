"""仪表盘的图表类片段：KPI 卡、调用趋势 SVG、合作伙伴 TOP 5 条形。

用户输入（伙伴名）全部 html.escape，片段里不存在未转义数据，
dashboard.js 轮询 innerHTML 替换时不会成为 XSS 入口。
"""

from __future__ import annotations

from html import escape

from ._format import _num


def _delta_html(pct) -> str:
    """环比：涨跌用箭头 + 符号表达，颜色只表意（涨=绿、跌=红、无参照=灰）"""
    if pct is None:
        return '<span class="delta flat">上一周期无数据</span>'
    tone = "good" if pct > 0 else ("bad" if pct < 0 else "flat")
    arrow = "M4 1L7 6H1Z" if pct >= 0 else "M4 7L1 2H7Z"
    return (
        f'<span class="delta {tone}">'
        f'<svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">'
        f'<path d="{arrow}" fill="currentColor"/></svg>{pct:+.1f}%</span>'
    )


def kpi_html(ov: dict) -> str:
    k = ov["kpi"]
    r = ov["range"]
    new_hint = f"区间新增 {k['partners_new']} 家" if k["partners_new"] else "本区间无新增伙伴"
    failed_hint = (
        f'<a href="/records?status=failed&amp;start={r["start"]}&amp;end={r["end"]}">失败 {k["failed"]}</a>'
        if k["failed"]
        else f'失败 {k["failed"]}'
    )
    return "".join(
        [
            '<div class="kpi"><div class="label">合作伙伴</div>'
            f'<div class="value">{k["partners_active"]}<span class="unit">/ {k["partners_total"]} 家</span></div>'
            f'<div class="hint">{new_hint}</div></div>',
            '<div class="kpi"><div class="label">模板调用</div>'
            f'<div class="value">{_num(k["calls"])}<span class="unit">次</span></div>'
            f'<div class="hint">{_delta_html(k["calls_delta_pct"])} 环比上一周期</div></div>',
            '<div class="kpi"><div class="label">成功率</div>'
            f'<div class="value">{k["success_rate"]:.1f}<span class="unit">%</span></div>'
            f'<div class="hint">成功 {k["success"]} · {failed_hint}</div></div>',
            '<div class="kpi"><div class="label">活跃员工</div>'
            f'<div class="value">{k["employees_active"]}<span class="unit">人</span></div>'
            f'<div class="hint">区间使用模板 {k["templates_active"]} 个</div></div>',
        ]
    )


def _short_date(iso: str) -> str:
    """2026-10-24 → 10-24。完整区间（带年份）在卡片标题里，不怕跨年认错。"""
    return iso[5:] if len(iso) >= 10 else iso


def _trend_svg(series: list[dict], peak_count: int) -> str:
    n = len(series)
    x0, x1 = 36.0, 550.0
    y0, y1 = 14.0, 154.0
    vmax = max(1, peak_count)
    span = y1 - y0
    mid = (x0 + x1) / 2

    parts = [
        '<svg class="trend-svg" viewBox="0 0 560 180" role="img" preserveAspectRatio="xMidYMid meet"',
        f' aria-label="区间每日调用次数趋势，{series[0]["date"]} 至 {series[-1]["date"]}，峰值 {_num(peak_count)} 次">',
        '<defs><linearGradient id="trendFade" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="var(--bar-from)" stop-opacity="0.30"/>'
        '<stop offset="100%" stop-color="var(--bar-from)" stop-opacity="0.02"/>'
        "</linearGradient></defs>",
    ]

    for v in (vmax, vmax / 2, 0):
        y = y1 - (v / vmax) * span
        parts.append(f'<line class="trend-grid" x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}"/>')
        parts.append(
            f'<text class="trend-ylbl" x="{x0 - 8}" y="{y + 3:.0f}" text-anchor="end">{v:g}</text>'
        )

    if n == 1:
        # 单日区间：画一根柱子，比一个孤零零的点诚实
        v = series[0]["count"]
        h = max(3.0, v / vmax * span)
        parts.append(
            f'<rect x="{mid - 18:.1f}" y="{y1 - h:.1f}" width="36" height="{h:.1f}" rx="4" fill="var(--bar-to)">'
            f'<title>{_short_date(series[0]["date"])} · {_num(v)} 次</title></rect>'
        )
        parts.append(
            f'<text class="trend-val" x="{mid:.0f}" y="{y1 - h - 6:.0f}" text-anchor="middle">{_num(v)}</text>'
        )
        parts.append(
            f'<text class="trend-lbl" x="{mid:.0f}" y="{y1 + 18:.0f}" text-anchor="middle">{_short_date(series[0]["date"])}</text>'
        )
        parts.append("</svg>")
        return "".join(parts)

    pts = []
    for i, p in enumerate(series):
        x = x0 + (x1 - x0) * i / (n - 1)
        y = y1 - (p["count"] / vmax) * span
        pts.append((x, y))
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    parts.append(f'<path d="{line} L{x1},{y1} L{x0},{y1} Z" fill="url(#trendFade)"/>')
    parts.append(
        f'<path d="{line}" fill="none" stroke="var(--bar-to)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
    )
    parts.append(f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="3" fill="var(--bar-to)"/>')

    # x 轴标签抽稀到 7 个以内、首尾必有，挤成一排就什么都看不清
    idxs = list(range(n)) if n <= 7 else sorted({round(i * (n - 1) / 6) for i in range(7)})
    for i in idxs:
        parts.append(
            f'<text class="trend-lbl" x="{pts[i][0]:.1f}" y="{y1 + 18:.0f}" text-anchor="middle">{_short_date(series[i]["date"])}</text>'
        )
    # 悬浮看单日数值：透明热区 + 原生 tooltip，不引图表库；放最后确保压在最上层
    for i, (x, y) in enumerate(pts):
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="transparent" pointer-events="all">'
            f'<title>{_short_date(series[i]["date"])} · {_num(series[i]["count"])} 次</title></circle>'
        )
    parts.append("</svg>")
    return "".join(parts)


def trend_html(ov: dict) -> str:
    k = ov["kpi"]
    r = ov["range"]
    head = (
        f'<h2>调用趋势 <span class="card-meta">{escape(r["label"])} · {r["start"]} ~ {r["end"]} · '
        f'日均 {k["avg_per_day"]:g} 次 · 峰值 {_num(k["peak_count"])}</span></h2>'
    )
    if k["calls"] == 0:
        return head + '<div class="trend-empty">本区间暂无调用记录</div>'
    return head + _trend_svg(ov["trend"], k["peak_count"])


def top_html(ov: dict) -> str:
    partners = ov["partners"][:5]
    r = ov["range"]
    head = (
        f'<h2>合作伙伴调用 TOP 5 <span class="card-meta">{escape(r["label"])} · '
        f'{r["start"]} ~ {r["end"]}</span></h2>'
    )
    if not partners:
        return head + '<div class="empty tight">本区间暂无调用记录</div>'
    top = partners[0]["calls"] or 1
    rows = []
    for p in partners:
        width = round(p["calls"] / top * 100, 1)
        # 可点击 + 键盘可达：跳到明细表对应行并展开（dashboard.js 委托处理）
        rows.append(
            f'<div class="bar-row top-row" role="button" tabindex="0" data-partner="{escape(p["name"])}"'
            f' aria-label="定位到明细表中的 {escape(p["name"])}" title="点击定位到明细表中的该伙伴">'
            f'<div class="name">{escape(p["name"])}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill" style="width: {width}%"></div></div>'
            f'<div class="val">{_num(p["calls"])}</div>'
            '</div>'
        )
    return head + '<div class="bars">' + "".join(rows) + '</div>'
