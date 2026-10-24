"""仪表盘的合作伙伴明细表：主表、行展开的模板明细、口径注脚。

用户输入（伙伴名 / 模板名）全部 html.escape 后进片段，
innerHTML 替换时不会成为 XSS 入口。
"""

from __future__ import annotations

from html import escape
from urllib.parse import quote

from ._format import _num

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
