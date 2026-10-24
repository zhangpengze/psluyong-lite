"""仪表盘概览导出：汇总 + 伙伴明细 + 各伙伴模板用量，一张表三级都在。"""

from __future__ import annotations

import csv
import io


def _csv_safe(value) -> str:
    """防 CSV 公式注入：Excel 会把 =/+/-/@ 开头的单元格当公式执行。

    与 routes/records_csv.py 的同名函数同规则——数据源有客户端上报内容，
    这条不是洁癖：伙伴名 / 模板名里真可能出现 = 开头的字符串。
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "\t" + text
    return text


def overview_csv(ov: dict) -> str:
    """概览导出：汇总 + 伙伴明细 + 各伙伴模板用量。

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
