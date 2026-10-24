"""日报落盘：Markdown 存档与推送状态 JSON（拆自 report.py）。

后台"每日日报"页读的就是这两个文件，所以两处都用原子写，
读方永远不会看到半截内容。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


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
