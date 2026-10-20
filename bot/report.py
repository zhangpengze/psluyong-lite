"""
日报生成与推送

设计取舍：
  - 报告先落盘成 Markdown（data/reports/YYYY-MM-DD.md），再尝试推送。
    推送失败不影响存档，后台"每日日报"页仍能看到当天数据。
  - 推送走 NapCat / OneBot v11 的 send_group_msg HTTP 接口。
    未配置 NAPCAT_WEBHOOK 时只落盘 + 记日志，不报错——
    这样"先跑通主链路、后接机器人"不会卡住交付。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func

logger = logging.getLogger(__name__)

# 推送重试：NapCat 抖动（重启中/网络抖一下）时当天不丢。
# 间隔按次递增，测试里可 monkeypatch 成 (0, 0)。
PUSH_ATTEMPTS = 3
PUSH_BACKOFF_S = (5, 15)


def build_daily_report(app, day: date | None = None) -> str:
    """生成某天的 Markdown 日报"""
    from server.app.db import db
    from server.app.models import STATUS_FAILED, Record

    day = day or date.today()

    with app.app_context():
        base = db.session.query(func.count(Record.id)).filter(
            Record.is_deleted.is_(False),
            Record.work_date == day,
        )
        total = base.scalar() or 0
        failed = base.filter(Record.status == STATUS_FAILED).scalar() or 0

        def group(column, limit: int = 10):
            rows = (
                db.session.query(column, func.count(Record.id))
                .filter(
                    Record.is_deleted.is_(False),
                    Record.work_date == day,
                    column.isnot(None),
                    column != "",
                )
                .group_by(column)
                .order_by(func.count(Record.id).desc())
                .limit(limit)
                .all()
            )
            return [(name, count) for name, count in rows]

        by_employee = group(Record.employee_name)
        by_template = group(Record.template_name)
        by_partner = group(Record.partner_name)

        failed_rows = (
            Record.query.filter(
                Record.is_deleted.is_(False),
                Record.work_date == day,
                Record.status == STATUS_FAILED,
            )
            .order_by(Record.created_at.desc())
            .limit(5)
            .all()
        )

    lines = [
        f"【{day:%Y-%m-%d} 期刊录用日报】",
        "",
        f"总录用：{total} 条　成功：{total - failed}　失败：{failed}"
        + (f"　成功率：{(total - failed) / total * 100:.1f}%" if total else ""),
        "",
    ]

    if by_employee:
        lines.append("按员工：")
        lines += [f"  · {name}：{count} 条" for name, count in by_employee]
        lines.append("")

    if by_template:
        lines.append("按模板：")
        lines += [f"  · {name}：{count} 条" for name, count in by_template]
        lines.append("")

    if by_partner:
        lines.append("按合作伙伴：")
        lines += [f"  · {name}：{count} 条" for name, count in by_partner]
        lines.append("")

    if failed_rows:
        lines.append(f"失败明细（最近 {len(failed_rows)} 条）：")
        for r in failed_rows:
            reason = (r.error_message or "未知原因")[:60]
            lines.append(f"  · {r.employee_name}｜{r.author}/{r.title}：{reason}")
        lines.append("")

    if total == 0:
        lines.append("今天没有录用记录。")

    lines.append(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    return "\n".join(lines)


def save_report(app, content: str, day: date) -> Path:
    from server.app.config import DATA_DIR

    report_dir = DATA_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{day:%Y-%m-%d}.md"
    path.write_text(content, encoding="utf-8")
    logger.info("日报已落盘 %s", path)
    return path


def push_to_group(content: str) -> tuple[bool, str]:
    """推送到 QQ 群。未配置视为"跳过"而非失败；失败自动重试。"""
    from server.app.config import Config

    webhook = Config.NAPCAT_WEBHOOK.rstrip("/")
    if not webhook:
        logger.info("未配置 NAPCAT_WEBHOOK，跳过推送（日报已存档）")
        return False, "未配置 NAPCAT_WEBHOOK，已跳过推送"
    if not Config.REPORT_GROUP_ID:
        logger.warning("已配置 NAPCAT_WEBHOOK 但缺少 REPORT_GROUP_ID，跳过推送")
        return False, "缺少 REPORT_GROUP_ID，已跳过推送"

    import requests

    headers = {"Content-Type": "application/json"}
    if Config.NAPCAT_TOKEN:
        headers["Authorization"] = f"Bearer {Config.NAPCAT_TOKEN}"

    payload = {
        "group_id": int(Config.REPORT_GROUP_ID) if Config.REPORT_GROUP_ID.isdigit() else Config.REPORT_GROUP_ID,
        "message": [{"type": "text", "data": {"text": content}}],
    }

    last = "未知错误"
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        try:
            resp = requests.post(f"{webhook}/send_group_msg", json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                logger.info("日报已推送 QQ 群 %s", Config.REPORT_GROUP_ID)
                return True, "推送成功"
            last = f"推送失败 HTTP {resp.status_code}"
            logger.error("第 %d/%d 次推送失败: %s %s", attempt, PUSH_ATTEMPTS, resp.status_code, resp.text[:200])
        except requests.RequestException as e:
            last = f"推送异常: {e}"
            logger.error("第 %d/%d 次推送异常: %s", attempt, PUSH_ATTEMPTS, e)
        if attempt < PUSH_ATTEMPTS:
            wait = PUSH_BACKOFF_S[min(attempt - 1, len(PUSH_BACKOFF_S) - 1)]
            logger.warning("%ds 后重试推送", wait)
            time.sleep(wait)
    return False, f"{last}（已重试 {PUSH_ATTEMPTS} 次）"


def save_push_status(day: date, status: dict) -> Path:
    """推送状态落盘到 reports/<日期>.status.json。

    后台日报页据此显示「已推送/未推送」徽标；补推后同一文件被覆盖更新。
    原子写：读方（网页渲染）永远不会看到半截 JSON。
    """
    from server.app.config import DATA_DIR

    status_dir = DATA_DIR / "reports"
    status_dir.mkdir(parents=True, exist_ok=True)
    path = status_dir / f"{day:%Y-%m-%d}.status.json"
    payload = dict(status)
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    logger.info("推送状态已记录 %s: pushed=%s", path.name, payload.get("pushed"))
    return path


def run_daily(app, day: date | None = None, push: bool = True) -> dict:
    """生成 + 存档 + 推送 + 记录推送状态。返回结构化结果供接口/定时任务使用。"""
    day = day or date.today()
    content = build_daily_report(app, day)
    path = save_report(app, content, day)
    pushed, message = (False, "未推送") if not push else push_to_group(content)
    save_push_status(day, {
        "date": day.isoformat(),
        "pushed": pushed,
        "message": message,
        "pushed_at": datetime.now().isoformat(timespec="seconds") if pushed else None,
    })
    return {"date": day.isoformat(), "path": str(path), "pushed": pushed, "message": message, "content": content}


def yesterday() -> date:
    return date.today() - timedelta(days=1)
