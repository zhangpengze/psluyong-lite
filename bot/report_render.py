"""日报文本渲染（拆自 report.py，纯函数、无 IO）

调用方（bot/report.py）负责传 `partner_limit`——渲染这里不读模块级常量，
否则测试里 `monkeypatch.setattr(bot.report, "PARTNER_TEMPLATE_LIMIT", 3)`
这种打桩就对不上了。
"""

from __future__ import annotations

from datetime import datetime


def render(stats: dict, final: bool = False, partner_limit: int = 5) -> str:
    """把统计结果渲染成群消息文本。

    final=True 是「次日终版」：18:00 推的快报之后员工可能又补录了记录，
    标题要明确标出来，否则群里看到两条内容不同的同一天日报会以为是 bug。
    """
    start, end = stats["start"], stats["end"]
    total, failed = stats["total"], stats["failed"]
    multi = start != end

    if multi:
        title = f"【{start:%Y-%m-%d} ~ {end:%Y-%m-%d} 期刊录用汇总】"
    else:
        title = f"【{start:%Y-%m-%d} 期刊录用日报{'（终版）' if final else ''}】"
    lines = [
        title,
        "",
        f"总录用：{total} 条　成功：{total - failed}　失败：{failed}"
        + (f"　成功率：{(total - failed) / total * 100:.1f}%" if total else ""),
        "",
    ]

    # 区间版才需要按日拆分：只看合计数看不出是哪天忙哪天空
    if multi and stats["by_day"]:
        lines.append("按日：")
        lines += [f"  · {d:%m-%d}：{c} 条" for d, c in stats["by_day"]]
        lines.append("")

    if stats["by_template"]:
        lines.append("按模板：")
        lines += [f"  · {name}：{count} 条" for name, count in stats["by_template"]]
        lines.append("")

    if stats["by_partner"]:
        lines.append("按合作伙伴：")
        for name, count in stats["by_partner"]:
            lines.append(f"  · {name}：{count} 条")
            details = stats["partner_templates"].get(name, [])
            for tname, tcount in details[:partner_limit]:
                lines.append(f"    - {tname}：{tcount} 条")
            hidden = len(details) - partner_limit
            if hidden > 0:
                lines.append(f"    - 等 {hidden} 个模板")
        lines.append("")

    if stats["failed_rows"]:
        lines.append(f"失败明细（最近 {len(stats['failed_rows'])} 条）：")
        for r in stats["failed_rows"]:
            reason = (r.error_message or "未知原因")[:60]
            lines.append(f"  · {r.employee_name}｜{r.author}/{r.title}：{reason}")
        lines.append("")

    if total == 0:
        lines.append("这段时间没有录用记录。")

    if final:
        lines.append("说明：这是当天的终版，已包含 18:00 之后补录的记录。")

    lines.append(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    return "\n".join(lines)
