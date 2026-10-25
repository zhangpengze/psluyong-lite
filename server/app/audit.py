"""审计日志写入

一条原则：审计失败不能拖垮业务。记日志本身出错时只打 warning，
绝不让"删除记录"因为"写日志失败"而 500。
"""

from __future__ import annotations

import logging

from flask import g, request

from .auth import current_user
from .db import db

logger = logging.getLogger(__name__)

# 动作名 → 中文名（页面展示与筛选用同一份表，避免两处各写一遍）
ACTION_LABELS = {
    "delete_record": "删除记录",
    "restore_record": "恢复记录",
    "export_csv": "导出 CSV",
    "push_report": "重推日报",
    "run_report": "生成日报",
    # 群内指令已随 NapCat 一起删除，但历史库里还有 bot_command 的行——
    # 留着这条映射，老的审计记录在页面上才显示成人话而不是一个裸 key。
    "bot_command": "群内指令（已下线）",
    "login": "登录成功",
    "login_failed": "登录失败",
    "login_blocked": "登录被限流",
}


def actor_name() -> str:
    """当前操作者：会话用户优先，其次 API Key 调用"""
    user = current_user()
    if user is not None:
        return user.username
    if getattr(g, "auth_via", None) == "api_key":
        return "api_key"
    return "-"


def log_action(
    action: str,
    target: str = "",
    detail: str = "",
    actor: str | None = None,
    commit: bool = True,
) -> None:
    """写一条审计流水。

    actor 显式传入的场景：登录成功那一刻 session 刚建立、g.user 还缓存着
    登录前的 None，自动识别会记成 "-"，所以登录相关调用必须自己带上用户名。
    """
    from .models import AuditLog

    # 后台线程（如 QQ 机器人回调）没有请求上下文，request.remote_addr
    # 会抛 RuntimeError——此时 ip 记 None，别让整条审计因此丢掉
    try:
        ip = (request.remote_addr or "")[:64] or None
    except RuntimeError:
        ip = None

    try:
        entry = AuditLog(
            actor=(actor or actor_name())[:64],
            action=action[:32],
            target=(target or "")[:64] or None,
            detail=(detail or "")[:500] or None,
            ip=ip,
        )
        db.session.add(entry)
        if commit:
            db.session.commit()
    except Exception as e:  # noqa: BLE001 - 审计不能成为业务的单点故障
        logger.warning("审计日志写入失败 action=%s target=%s: %s", action, target, e)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
