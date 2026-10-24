"""日报文件读取：列表、推送状态、正文与日期反解。

reports 视图的文件系统逻辑全在这里。防目录穿越是安全边界，
只此一份实现——视图层不再直接摸文件路径。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from ..config import DATA_DIR

REPORT_DIR = DATA_DIR / "reports"


def list_reports(limit: int = 30) -> list[Path]:
    """最近的 .md 日报，新→旧。目录不存在返回空列表。"""
    if not REPORT_DIR.exists():
        return []
    return sorted(REPORT_DIR.glob("*.md"), reverse=True)[:limit]


def load_statuses(files: list[Path]) -> dict[str, dict]:
    """每条日报的推送状态（bot/report.save_push_status 落盘的同名 JSON）。

    老日报没有状态文件，不显示徽标——读失败按"无状态"吞掉。
    """
    statuses: dict[str, dict] = {}
    for f in files:
        status_file = REPORT_DIR / f"{f.stem}.status.json"
        try:
            statuses[f.stem] = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return statuses


def resolve_content(selected: str, files: list[Path]) -> tuple[str, str]:
    """取选中日报的正文，返回 (selected, content)。

    防目录穿越：只允许 REPORT_DIR 下的 .md（后缀 + resolve 后父目录双重校验）。
    校验不过或文件不存在时 content 留空、selected 原样返回——页面照常渲染，
    只是没正文，绝不把路径外的文件读出来。
    """
    if selected:
        candidate = REPORT_DIR / selected
        if candidate.suffix == ".md" and candidate.parent.resolve() == REPORT_DIR.resolve():
            if candidate.exists():
                return selected, candidate.read_text(encoding="utf-8")
        return selected, ""
    if files:
        return files[0].name, files[0].read_text(encoding="utf-8")
    return "", ""


def parse_report_date(selected: str) -> date | None:
    """重推时用文件名反解日期，解不出来返回 None（视图据此不给按钮）——
    比让接口悄悄改成"今天就今天"强，那样会把错误的日期推给全公司。
    """
    if not selected:
        return None
    try:
        return datetime.strptime(Path(selected).stem, "%Y-%m-%d").date()
    except ValueError:
        return None
