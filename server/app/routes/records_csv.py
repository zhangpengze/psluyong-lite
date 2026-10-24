"""记录导出的 CSV 组装（纯函数：rows → CSV 文本）。

归属表现层而非 service：service 只负责取数（records_read.list_for_export），
这里拼的是响应体格式。单独成文件是为了让路由文件只留"取参数 → 取数 → 回响应"
三步，且表头清单有独立审计记录（B3 / D1 方案 A），不该埋在端点函数中间。
"""

from __future__ import annotations

import csv
import io

from ..models import STATUS_SUCCESS

# CSV 导出列（顺序即表头）。中文表头给 Excel 用户看，列名保持与模型字段一致
_EXPORT_HEADERS = [
    ("work_date", "工作日期"),
    ("created_at", "提交时间"),
    ("employee_name", "员工"),
    ("template_name", "模板"),
    ("partner_name", "合作伙伴"),
    ("author", "作者"),
    ("title", "标题"),
    ("processing_ms", "耗时(秒)"),
    ("status", "状态"),
    ("error_message", "错误信息"),
    ("client_machine", "提交机器"),
    ("ps_version", "PS版本"),
    # 曾有「图片路径」列：Record.to_dict() 不含该键，列头有、数据恒空（审计 B3）。
    # 按 D1 方案 A 删列——导出文件也不该外泄服务器文件系统路径。
]


def _csv_safe(value) -> str:
    """防 CSV 公式注入：Excel 会把 =/+/-/@ 开头的单元格当公式执行。

    前置一个制表符（不可见），单元格按文本处理。数据源有客户端上报内容，
    这条不是洁癖——员工名/标题里真可能出现 = 开头的字符串。
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "\t" + text
    return text


def write_records_csv(rows) -> str:
    """整表 CSV 文本：UTF-8 BOM + 表头 + 数据行（CRLF，Excel 友好）。"""
    buf = io.StringIO()
    # utf-8-sig 即带 BOM 的 UTF-8：Excel 靠 BOM 识别中文编码，不加必乱码
    buf.write("\ufeff")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([header for _col, header in _EXPORT_HEADERS])

    for r in rows:
        data = r.to_dict()
        row = []
        for col, _header in _EXPORT_HEADERS:
            value = data.get(col)
            if col == "processing_ms" and value:
                value = round(value / 1000, 1)  # 列头已注明单位是秒
            elif col == "status":
                value = "成功" if value == STATUS_SUCCESS else "失败"
            row.append(_csv_safe(value))
        writer.writerow(row)
    return buf.getvalue()
