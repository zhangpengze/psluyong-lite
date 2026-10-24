"""仪表盘的导出渲染：HTML 片段 + CSV。

为什么由服务端渲染、不让 JS 拼 DOM：
    页面首屏要服务端直出（无 JS / 弱网也能看），轮询更新又要同一套标记。
    两套渲染必然漂移，所以只保留一套——Python 生成片段，Jinja 用 |safe
    输出首屏，dashboard.js 轮询时 innerHTML 原地替换。
    用户输入（伙伴名 / 模板名）全部 html.escape，片段里不存在未转义数据，
    innerHTML 不会成为 XSS 入口。
"""

from __future__ import annotations

import csv
import io
from html import escape
from urllib.parse import quote


def _num(value) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


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


_TABLE_HEAD = (
    '<div class="table-wrap"><table>'
    '<caption class="sr-only">合作伙伴明细：模板数、区间调用、占比、最近活跃与状态，点行展开各模板用量</caption>'
    '<thead><tr>'
    '<th scope="col">合作伙伴</th>'
    '<th scope="col" class="r">模板数</th>'
    '<th scope="col" class="r">区间调用</th>'
    '<th scope="col">占比</th>'
    '<th scope="col">最近活跃</th>'
    '<th scope="col">状态</th>'
    "</tr></thead><tbody>"
)


def table_html(ov: dict, limit: int = 20) -> str:
    """伙伴明细表。limit=0 出全部；否则只出前 limit 家 + 「显示全部」按钮
    （数据全在响应里，按钮只是让 JS 带 limit=0 再取一次，避免首屏臃肿）。"""
    partners = ov["partners"]
    if not partners:
        return (
            '<div class="empty"><div class="empty-title">本区间暂无调用记录</div>'
            "<div>员工客户端生成后会自动出现在这里，也可换个时间范围看看</div></div>"
        )
    shown = partners if limit == 0 else partners[:limit]
    rows = []
    for i, p in enumerate(shown):
        rows.append(
            f'<tr class="partner-row" tabindex="0" data-idx="{i}" data-name="{escape(p["name"])}" aria-expanded="false">'
            f'<td class="pname">{escape(p["name"])}</td>'
            f'<td class="r num">{p["templates_used"]}</td>'
            f'<td class="r num">{_num(p["calls"])}</td>'
            f'<td class="share-cell"><div class="share-flex"><div class="bar-track"><div class="bar-fill" style="width: {p["share"]}%"></div></div>'
            f'<span class="share-num">{p["share"]:.1f}%</span></div></td>'
            f'<td class="num muted">{p["last_active"] or "-"}</td>'
            f'<td><span class="ptag {p["status"]}"><span class="dot"></span>{p["status_label"]}</span></td>'
            "</tr>"
        )
    table = _TABLE_HEAD + "".join(rows) + "</tbody></table></div>"
    if limit and len(partners) > limit:
        hidden = len(partners) - limit
        table += (
            '<div class="table-more">'
            f'<button type="button" class="btn ghost sm" id="show-more-partners">'
            f'显示全部 {len(partners)} 家（还有 {hidden} 家未显示）</button></div>'
        )
    return table


def detail_html(ov: dict, p: dict) -> str:
    """伙伴行展开后的模板明细（随首屏数据一起下发，展开不发请求）"""
    r = ov["range"]
    href = f"/records?partner={quote(p['name'])}&amp;start={r['start']}&amp;end={r['end']}"
    rows = "".join(
        f'<tr><td>{escape(t["name"])}</td>'
        f'<td class="r num">{_num(t["calls"])}</td>'
        f'<td class="r num">{t["share"]:.1f}%</td></tr>'
        for t in p["templates"]
    )
    return (
        '<tr class="detail-row"><td colspan="6"><div class="detail-body">'
        f'<div class="tpl-head"><span class="tpl-sum">{escape(p["name"])} · {escape(r["label"])}'
        f'共用 {p["templates_used"]} 个模板 · {_num(p["calls"])} 次</span>'
        f'<a href="{href}">查看该伙伴全部记录</a></div>'
        '<table class="tpl-table"><thead><tr><th>模板</th>'
        '<th class="r">调用次数</th><th class="r">占该伙伴</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
        "</div></td></tr>"
    )


def note_html(ov: dict, limit: int = 20) -> str:
    k = ov["kpi"]
    parts = [f'状态口径：{ov["meta"]["status_rule"]}']
    if k["partners_inactive"] > 0:
        parts.append(f'另有 {k["partners_inactive"]} 家伙伴本区间无调用')
    if limit and len(ov["partners"]) > limit:
        parts.append(f'表格按调用排序仅显示前 {limit} 家，点「显示全部」看齐')
    parts.append("点伙伴行展开，看每个模板用了几次")
    parts.append(f'调用次数口径：{ov["meta"]["calls_definition"]}')
    return '<div class="table-note">' + "｜".join(parts) + "</div>"


# ---------- 系统状态卡（组件行式，参考云厂商状态页） ----------

_STATUS_WORD = {"ok": "正常", "warn": "警告", "bad": "异常", "off": "未配置"}


def status_html(s: dict) -> str:
    """一张卡五行组件：状态点 + 组件名 · 状态 + 右侧真实指标。

    行的数据与分档在 server/app/status.py；这里只负责标记。
    """
    from .status import SYS_COMPONENTS

    rows = []
    for key, label in SYS_COMPONENTS:
        row = s["rows"].get(key) or {"status": "off", "detail": "-"}
        tone = row["status"] if row["status"] in _STATUS_WORD else "off"
        rows.append(
            '<div class="sys-row">'
            f'<span class="sys-dot {tone}" aria-hidden="true"></span>'
            f'<span class="sys-name">{label}</span>'
            f'<span class="sys-status {tone}">· {_STATUS_WORD[tone]}</span>'
            f'<span class="sys-metric">{escape(row.get("detail") or "-")}</span>'
            "</div>"
        )
    return (
        '<div class="card"><h2>系统状态<a class="card-link" href="/audit">操作日志</a></h2>'
        '<div class="sys-rows">' + "".join(rows) + "</div></div>"
    )


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


# ---------- CSV 导出 ----------

def _csv_safe(value) -> str:
    """防 CSV 公式注入：Excel 会把 =/+/-/@ 开头的单元格当公式执行。

    与 routes/records.py 的同名函数同规则——数据源有客户端上报内容，
    这条不是洁癖：伙伴名 / 模板名里真可能出现 = 开头的字符串。
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "\t" + text
    return text


def overview_csv(ov: dict) -> str:
    """概览导出：汇总 + 伙伴明细 + 各伙伴模板用量，一张表三级都在。

    utf-8-sig（BOM）：Excel 靠 BOM 识别中文编码，不加必乱码——
    与记录页导出同一套处理。导出不受 limit 截断影响，永远是全量。
    """
    k = ov["kpi"]
    r = ov["range"]
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, lineterminator="\r\n")

    delta = (
        f"{k['calls_delta_pct']:+.1f}%"
        if k["calls_delta_pct"] is not None
        else "上一周期无数据"
    )
    writer.writerow(["数据概览导出", f"{r['start']} ~ {r['end']}", r["label"]])
    writer.writerow(["导出时间", ov["updated_iso"]])
    writer.writerow([])
    writer.writerow(["汇总"])
    writer.writerow(["合作伙伴（活跃/总数）", f"{k['partners_active']} / {k['partners_total']} 家"])
    writer.writerow(["区间新增伙伴", k["partners_new"]])
    writer.writerow(["模板调用", k["calls"]])
    writer.writerow(["环比上一周期", delta])
    writer.writerow(["成功 / 失败", f"{k['success']} / {k['failed']}"])
    writer.writerow(["成功率", f"{k['success_rate']}%"])
    writer.writerow(["活跃员工", k["employees_active"]])
    writer.writerow(["使用模板", k["templates_active"]])
    writer.writerow(["日均调用", k["avg_per_day"]])
    writer.writerow([])
    writer.writerow(["合作伙伴明细"])
    writer.writerow(["合作伙伴", "模板数", "区间调用", "占比(%)", "最近活跃", "状态"])
    for p in ov["partners"]:
        writer.writerow(
            [
                _csv_safe(p["name"]),
                p["templates_used"],
                p["calls"],
                p["share"],
                p["last_active"] or "-",
                p["status_label"],
            ]
        )
    writer.writerow([])
    writer.writerow(["各伙伴模板用量"])
    writer.writerow(["合作伙伴", "模板", "调用次数", "占该伙伴(%)"])
    for p in ov["partners"]:
        for t in p["templates"]:
            writer.writerow([_csv_safe(p["name"]), _csv_safe(t["name"]), t["calls"], t["share"]])
    writer.writerow([])
    writer.writerow(["口径", ov["meta"]["calls_definition"], "业务日", ov["meta"]["business_date"]])
    return buf.getvalue()
