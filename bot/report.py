"""
日报生成与推送

设计取舍：
  - 报告先落盘成 Markdown（data/reports/YYYY-MM-DD.md），再尝试推送。
    推送失败不影响存档，后台"每日日报"页仍能看到当天数据。
  - 推送走 NapCat / OneBot v11 的 send_group_msg HTTP 接口。
    未配置 NAPCAT_WEBHOOK 时只落盘 + 记日志，不报错——
  - 单日与区间（本周/本月）共用一套「_collect_stats + _render」：
    区间版只多一个「按日」段落，统计口径完全一致。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func

logger = logging.getLogger(__name__)

# 推送重试：NapCat 抖动（重启中/网络抖一下）时当天不丢。
# 间隔按次递增，测试里可 monkeypatch 成 (0, 0)。
PUSH_ATTEMPTS = 3
PUSH_BACKOFF_S = (5, 15)


# 每个合作伙伴下面最多展开几个模板，防止某个伙伴模板太多把日报撑爆
PARTNER_TEMPLATE_LIMIT = 5


def _collect_stats(app, start: date, end: date) -> dict:
    """区间统计。单日日报是 start == end 的特例。"""
    from server.app.db import db
    from server.app.models import STATUS_FAILED, Record

    with app.app_context():
        base = db.session.query(func.count(Record.id)).filter(
            Record.is_deleted.is_(False),
            Record.work_date >= start,
            Record.work_date <= end,
        )
        total = base.scalar() or 0
        failed = base.filter(Record.status == STATUS_FAILED).scalar() or 0

        def group(column, limit: int = 10):
            rows = (
                db.session.query(column, func.count(Record.id))
                .filter(
                    Record.is_deleted.is_(False),
                    Record.work_date >= start,
                    Record.work_date <= end,
                    column.isnot(None),
                    column != "",
                )
                .group_by(column)
                .order_by(func.count(Record.id).desc())
                .limit(limit)
                .all()
            )
            return [(name, count) for name, count in rows]

        by_template = group(Record.template_name)
        by_partner = group(Record.partner_name)

        # 合作伙伴 × 模板 二维分布：按合作伙伴段落里每个伙伴下面展开，
        # 让「农技推广中心 7 条」直接看到是农业加急 3 + 今日农业 4
        pt_rows = (
            db.session.query(Record.partner_name, Record.template_name, func.count(Record.id))
            .filter(
                Record.is_deleted.is_(False),
                Record.work_date >= start,
                Record.work_date <= end,
                Record.partner_name.isnot(None),
                Record.partner_name != "",
            )
            .group_by(Record.partner_name, Record.template_name)
            .all()
        )

        by_day = (
            db.session.query(Record.work_date, func.count(Record.id))
            .filter(Record.is_deleted.is_(False), Record.work_date >= start, Record.work_date <= end)
            .group_by(Record.work_date)
            .order_by(Record.work_date.desc())
            .all()
        )

        failed_rows = (
            Record.query.filter(
                Record.is_deleted.is_(False),
                Record.work_date >= start,
                Record.work_date <= end,
                Record.status == STATUS_FAILED,
            )
            .order_by(Record.created_at.desc())
            .limit(5)
            .all()
        )

    # partner -> [(template, count)]，按数量降序；模板名空的归「未填模板」保证总数守恒
    partner_templates: dict[str, list[tuple[str, int]]] = {}
    for partner, template, count in pt_rows:
        partner_templates.setdefault(partner, []).append((template or "未填模板", count))
    for rows in partner_templates.values():
        rows.sort(key=lambda item: (-item[1], item[0]))

    return {
        "start": start,
        "end": end,
        "total": total,
        "failed": failed,
        "by_template": by_template,
        "by_partner": by_partner,
        "partner_templates": partner_templates,
        "by_day": [(d, c) for d, c in by_day],
        "failed_rows": failed_rows,
    }


def _render(stats: dict) -> str:
    start, end = stats["start"], stats["end"]
    total, failed = stats["total"], stats["failed"]
    multi = start != end

    title = (
        f"【{start:%Y-%m-%d} ~ {end:%Y-%m-%d} 期刊录用汇总】"
        if multi
        else f"【{start:%Y-%m-%d} 期刊录用日报】"
    )
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
            for tname, tcount in details[:PARTNER_TEMPLATE_LIMIT]:
                lines.append(f"    - {tname}：{tcount} 条")
            hidden = len(details) - PARTNER_TEMPLATE_LIMIT
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

    lines.append(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    return "\n".join(lines)


def build_daily_report(app, day: date | None = None) -> str:
    """生成某天的日报（区间版的特例，保持原签名）"""
    day = day or date.today()
    return _render(_collect_stats(app, day, day))


def build_period_report(app, start: date, end: date) -> str:
    """生成区间汇总日报（本周/本月/上周）。start > end 时自动交换。"""
    if start > end:
        start, end = end, start
    return _render(_collect_stats(app, start, end))


def save_report(app, content: str, day: date) -> Path:
    from server.app.config import DATA_DIR

    report_dir = DATA_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{day:%Y-%m-%d}.md"
    # 原子写：后台日报页随时在读这个文件，不能让它看到半截内容
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    logger.info("日报已落盘 %s", path)
    return path


def _post_with_retry(action: str, payload: dict, description: str) -> tuple[bool, str]:
    """带重试的 OneBot 动作调用。description 只用于日志，不带密钥。"""
    from server.app.config import Config

    webhook = Config.NAPCAT_WEBHOOK.rstrip("/")
    if not webhook:
        logger.info("未配置 NAPCAT_WEBHOOK，跳过%s（日报已存档）", description)
        return False, "未配置 NAPCAT_WEBHOOK，已跳过推送"
    if not Config.REPORT_GROUP_ID and action == "send_group_msg":
        logger.warning("已配置 NAPCAT_WEBHOOK 但缺少 REPORT_GROUP_ID，跳过%s", description)
        return False, "缺少 REPORT_GROUP_ID，已跳过推送"

    import requests

    headers = {"Content-Type": "application/json"}
    if Config.NAPCAT_TOKEN:
        headers["Authorization"] = f"Bearer {Config.NAPCAT_TOKEN}"

    last = "未知错误"
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        try:
            resp = requests.post(f"{webhook}/{action}", json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                logger.info("%s成功（%s）", description, action)
                return True, "推送成功"
            last = f"推送失败 HTTP {resp.status_code}"
            logger.error("第 %d/%d 次%s失败: %s %s", attempt, PUSH_ATTEMPTS, description, resp.status_code, resp.text[:200])
        except requests.RequestException as e:
            last = f"推送异常: {e}"
            logger.error("第 %d/%d 次%s异常: %s", attempt, PUSH_ATTEMPTS, description, e)
        if attempt < PUSH_ATTEMPTS:
            wait = PUSH_BACKOFF_S[min(attempt - 1, len(PUSH_BACKOFF_S) - 1)]
            logger.warning("%ds 后重试%s", wait, description)
            time.sleep(wait)
    return False, f"{last}（已重试 {PUSH_ATTEMPTS} 次）"


def push_to_group(content: str) -> tuple[bool, str]:
    """推送到 QQ 群。未配置视为"跳过"而非失败；失败自动重试。"""
    from server.app.config import Config

    group_id = Config.REPORT_GROUP_ID
    payload = {
        "group_id": int(group_id) if group_id.isdigit() else group_id,
        "message": [{"type": "text", "data": {"text": content}}],
    }
    return _post_with_retry("send_group_msg", payload, "日报推送")


def push_to_user(user_id: int | str, content: str) -> tuple[bool, str]:
    """私聊推送给单个 QQ 号（群内触发指令时回复触发者用）。

    与 push_to_group 共用重试逻辑；未配置 NAPCAT_WEBHOOK 时同样只跳过。
    """
    payload = {
        "user_id": int(user_id) if str(user_id).isdigit() else user_id,
        "message": [{"type": "text", "data": {"text": content}}],
    }
    return _post_with_retry("send_private_msg", payload, f"私聊推送 {user_id}")


# 失败告警的最小间隔门控：PS 挂了员工连点 5 次就是 5 条失败记录，
# 不设限会把群刷爆。值取 Config.ALERT_MIN_INTERVAL_S，测试里可调。
_alert_lock = threading.Lock()
_last_alert_at: float = 0.0


def reset_alert_throttle() -> None:
    """测试用：清空告警门控"""
    global _last_alert_at
    with _alert_lock:
        _last_alert_at = 0.0


def push_alert(content: str, min_interval_s: int | None = None) -> tuple[bool, str]:
    """录用失败即时告警：推到 REPORT_GROUP_ID。

    未配置 NapCat 时静默跳过（本地开发/未接机器人时不该报错）；
    间隔内的重复告警被抑制——失败记录本身已落库，告警只是通知。
    """
    from server.app.config import Config

    global _last_alert_at
    interval = Config.ALERT_MIN_INTERVAL_S if min_interval_s is None else min_interval_s

    if not Config.NAPCAT_WEBHOOK:
        return False, "未配置 NAPCAT_WEBHOOK，跳过告警"
    if not Config.REPORT_GROUP_ID:
        return False, "缺少 REPORT_GROUP_ID，跳过告警"

    now = time.monotonic()
    with _alert_lock:
        if now - _last_alert_at < interval:
            return False, f"告警间隔小于 {interval}s，已抑制"
        _last_alert_at = now

    group_id = Config.REPORT_GROUP_ID
    payload = {
        "group_id": int(group_id) if group_id.isdigit() else group_id,
        "message": [{"type": "text", "data": {"text": content}}],
    }
    return _post_with_retry("send_group_msg", payload, "失败告警")


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


def run_daily(app, day: date | None = None, push: bool = True, record_status: bool = True) -> dict:
    """生成 + 存档 + 推送 + 记录推送状态。返回结构化结果供接口/定时任务使用。

    record_status=False 时不覆盖 reports/<日期>.status.json——
    群内指令触发（不推群）不能把定时推送的「已推送」徽标刷成「未推送」。
    """
    day = day or date.today()
    content = build_daily_report(app, day)
    path = save_report(app, content, day)
    pushed, message = (False, "未推送") if not push else push_to_group(content)
    if record_status:
        save_push_status(day, {
            "date": day.isoformat(),
            "pushed": pushed,
            "message": message,
            "pushed_at": datetime.now().isoformat(timespec="seconds") if pushed else None,
        })
    return {"date": day.isoformat(), "path": str(path), "pushed": pushed, "message": message, "content": content}


def yesterday() -> date:
    return date.today() - timedelta(days=1)
