"""日报巡检：次日补终版 + 找出更早的漏推

为什么必须有它（两个坑，都是"没人发现"型）：

1. **18:00 之后补录的记录进不了任何一份日报。** 18:00 推出去的是当日快照，
   而员工加班补录时 work_date 仍是当天。第二天早上重算一遍，数据变了才补推
   一份「（终版）」，没变则一条消息都不发。
2. **服务在 18:00 前后重启/停机时，当天可能一次都没推。** APScheduler 用的是
   MemoryJobStore，进程重启后 job 不存在，"misfire 补跑"救不了——只能靠下一次
   巡检发现"昨天没推成功"再补。

防刷屏策略：只自动补**昨天**。更早的缺口汇总成一条运维告警列出日期，
由管理员在后台「每日日报 → 重推此日报」按需补（那里支持任意日期）。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def missing_days(app, days: list[date]) -> list[str]:
    """筛出"该有日报却没推送成功"的日期。

    判据（缺一不可，避免把"还没部署/那天根本没开工"也算成事故）：
      - 这天没有推送成功的记录，且
      - 这天**生成过**日报（.md 在），或者**库里有记录**（有人确实干了活）
    """
    from server.app.config import DATA_DIR
    from server.app.db import db
    from server.app.models import Record

    from .report_store import load_push_status

    missing: list[str] = []
    for day in days:
        if load_push_status(day).get("pushed"):
            continue
        if (DATA_DIR / "reports" / f"{day:%Y-%m-%d}.md").exists():
            missing.append(day.isoformat())
            continue
        with app.app_context():
            has_rows = (
                db.session.query(Record.id)
                .filter(Record.is_deleted.is_(False), Record.work_date == day)
                .first()
                is not None
            )
        if has_rows:
            missing.append(day.isoformat())
    return missing


def patrol(app, today: date | None = None) -> dict:
    """每天固定时点跑一次：补昨天的终版，并把更早的缺口报出来。

    返回值给调度器包装层做告警判断用，不抛异常（单天失败不能让 job 死掉）。
    """
    from server.app.config import Config

    from .report import run_daily

    today = today or date.today()
    target = today - timedelta(days=1)
    result = run_daily(app, target, push=True, final=True)
    logger.info("日报终版巡检 %s：%s", target, result["message"])

    lookback = max(1, int(getattr(Config, "REPORT_MISS_LOOKBACK_DAYS", 7) or 1))
    # 从"前天"往前看：昨天归上面那次终版处理，不重复列
    older = [today - timedelta(days=n) for n in range(2, lookback + 1)]
    missing = missing_days(app, older)
    if missing:
        from .report_alert import push_ops_alert

        push_ops_alert(
            f"有 {len(missing)} 天日报没推送成功",
            "日期："
            + "、".join(missing)
            + "｜到后台「每日日报」选中该日期点「重推此日报」即可补推",
            kind="patrol",
        )
        logger.warning("日报缺口 %d 天：%s", len(missing), "、".join(missing))

    return {
        "date": target.isoformat(),
        "pushed": result["pushed"],
        "skipped": result.get("skipped", False),
        "message": result["message"],
        "missing": missing,
    }
