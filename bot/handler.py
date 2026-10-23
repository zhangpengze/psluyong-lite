"""QQ 机器人指令处理：监听群消息，关键词触发当天日报。

链路（反向）：
    QQ 群消息 → NapCat 反向上报 POST /api/v1/bot/onebot
    → 本模块解析/鉴权/冷却 → bot.report 生成日报 → 私聊回复触发者

设计取舍：
  - 只认精确指令名 + 可选日期参数（见 COMMANDS），不用包含匹配——
    「汇总」两个字在正常聊天里太容易出现，误触发会往群里/私聊里乱刷日报。
  - 群内触发只私聊回复触发者，不再推群（定时任务已经推过群了），
    且 run_daily 用 record_status=False，不覆盖定时推送的徽标状态。
  - 私聊失败（被拒收/不是好友）时 fallback 发回群里，至少触发者看得到。
  - 每次成功触发写一条审计（actor=qq:号），后台「操作日志」可追溯。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, timedelta

from .report import build_period_report, push_to_group, push_to_user

logger = logging.getLogger(__name__)

# 指令表：key 为消息首个词（空格分隔），value 为命令名
COMMANDS = {
    "汇总": "report",
    "日报": "report",
    "数据汇总": "report",
    "帮助": "help",
    "指令": "help",
}

# OneBot 群消息里的 CQ 码，如 [CQ:at,qq=123] [CQ:face,id=42]
_CQ_PATTERN = re.compile(r"\[CQ:[^\]]+\]")

HELP_TEXT = """【录用机器人指令】
汇总 — 今天的日报
汇总 昨天 / 前天 / 今天 — 近几天日报
汇总 MM-DD / 汇总 YYYY-MM-DD — 指定日期
汇总 本周 / 本月 / 上周 — 区间汇总
帮助 — 显示本列表"""

# 触发冷却：同一 user_id 在窗口内重复触发直接忽略（防连点/重复上报）
_cooldown: dict[int, float] = {}
_cooldown_lock = threading.Lock()


def extract_text(payload: dict) -> str:
    """从 OneBot v11 上报里提取纯文本。

    优先走 message 段数组（结构化、无 CQ 码），只拼接 text 段——
    at / face / image 等非文本段天然被过滤。退化场景用 raw_message，
    此时要剥掉 CQ 码。
    """
    segments = payload.get("message")
    if isinstance(segments, list):
        texts = [
            seg.get("data", {}).get("text", "")
            for seg in segments
            if isinstance(seg, dict) and seg.get("type") == "text"
        ]
        text = "".join(texts)
    else:
        text = str(payload.get("raw_message") or "")
    # raw_message 形态或段里残留的 CQ 码统一剥掉（如 [CQ:at,qq=xxx] 汇总）
    text = _CQ_PATTERN.sub("", text)
    # 全角空格等空白统一成半角后 strip
    return text.replace("\u3000", " ").strip()


def parse_range(arg: str, today: date | None = None) -> tuple[date, date] | None:
    """把指令参数解析成 (start, end)。无法识别返回 None。

    支持：昨天/前天/今天、本周/本月/上周、MM-DD、YYYY-MM-DD。
    MM-DD 取当前年；解析出来在未来则回退一年（12 月发「汇总 01-05」
    显然问的是今年 1 月，而不是明年）。
    """
    today = today or date.today()
    arg = arg.strip()
    if not arg:
        return today, today

    if arg == "昨天":
        d = today - timedelta(days=1)
        return d, d
    if arg == "前天":
        d = today - timedelta(days=2)
        return d, d
    if arg == "今天":
        return today, today

    if arg == "本周":
        start = today - timedelta(days=today.weekday())  # 周一
        return start, today
    if arg == "本月":
        return today.replace(day=1), today
    if arg == "上周":
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(days=7), monday - timedelta(days=1)

    # YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", arg)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, d
        except ValueError:
            return None
    # MM-DD（今年；在未来则去年）
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", arg)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        if d > today:
            d = date(today.year - 1, d.month, d.day)
        return d, d
    return None


def parse_command(text: str, today: date | None = None) -> tuple[str, dict] | None:
    """解析群消息。返回 (command, params)；非指令返回 None。

    report 的 params 含 start/end；日期参数无法识别时命令降级为 help，
    params.bad_arg 带上原始参数，调用方回复帮助文案。
    """
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None
    command = COMMANDS.get(parts[0])
    if command is None:
        return None
    arg = parts[1].strip() if len(parts) > 1 else ""
    if command == "help":
        return "help", {}
    rng = parse_range(arg, today)
    if rng is None:
        return "help", {"bad_arg": arg}
    return "report", {"start": rng[0], "end": rng[1]}


def _allowed(user_id: int, whitelist: set[int]) -> bool:
    """白名单为空 = 不限制（内网小群场景）；非空则只放行名单内 QQ 号。"""
    return not whitelist or user_id in whitelist


def _cooldown_ok(user_id: int, window_s: int) -> bool:
    now = time.monotonic()
    with _cooldown_lock:
        last = _cooldown.get(user_id)
        if last is not None and now - last < window_s:
            return False
        _cooldown[user_id] = now
        if len(_cooldown) > 64:  # 攒够了再打扫，不用每次加锁都扫全表
            stale_before = now - window_s * 10
            for uid, ts in list(_cooldown.items()):
                if ts < stale_before:
                    del _cooldown[uid]
        return True


def reset_cooldown() -> None:
    """测试用：清空冷却记录。"""
    with _cooldown_lock:
        _cooldown.clear()


def _audit_command(user_id: int, group_id, text: str) -> None:
    """群内指令写审计流水。失败只记日志，不影响回复。"""
    from server.app.audit import log_action

    log_action(
        "bot_command",
        target=f"group:{group_id}",
        detail=text[:200],
        actor=f"qq:{user_id}",
    )


def _reply(uid: int, group_id, content: str) -> tuple[bool, str]:
    """私聊回复触发者；失败 fallback 发回群里"""
    ok, message = push_to_user(uid, content)
    if ok:
        return True, f"已私聊回复 {uid}"
    logger.warning("私聊回复失败（%s），fallback 发回群 %s", message, group_id)
    ok2, message2 = push_to_group(content)
    if ok2:
        return True, f"私聊失败已转推群: {message2}"
    return False, f"私聊与推群均失败: {message2}"


def handle_event(app, payload: dict, whitelist: set[int] | None = None,
                 cooldown_s: int = 10) -> tuple[bool, str]:
    """处理一条 OneBot 上报。返回 (是否回复, 说明)。

    非消息事件、非群消息、非指令、不在白名单、冷却期内都会直接跳过，
    一条回复都不发—— NapCat 上报量不小，静默过滤是常态。
    """
    from server.app.config import Config

    whitelist = whitelist if whitelist is not None else _parse_whitelist(Config.BOT_ADMIN_QQ_IDS)
    cooldown_s = cooldown_s if cooldown_s is not None else Config.BOT_COOLDOWN_S

    if payload.get("post_type") != "message" or payload.get("message_type") != "group":
        return False, "非群消息事件"

    # 机器人自己的消息（echo）：NapCat 某些配置会把账号自身消息也上报，
    # 不防的话 fallback 推回去的内容可能被自己再消费
    if payload.get("self_id") is not None and payload.get("self_id") == payload.get("user_id"):
        return False, "自身消息回声"

    user_id = payload.get("user_id")
    group_id = payload.get("group_id")
    text = extract_text(payload)
    parsed = parse_command(text)
    if parsed is None:
        return False, "非指令消息"
    command, params = parsed

    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        logger.warning("群消息缺少合法 user_id: %r", user_id)
        return False, "user_id 异常"

    if not _allowed(uid, whitelist):
        logger.info("白名单外的触发已忽略: user_id=%s group_id=%s text=%r", uid, group_id, text)
        return False, "用户不在白名单"

    if not _cooldown_ok(uid, cooldown_s):
        logger.info("冷却期内重复触发已忽略: user_id=%s text=%r", uid, text)
        return False, "冷却期内"

    if command == "help":
        bad = params.get("bad_arg")
        content = f"无法识别日期参数「{bad}」。\n\n{HELP_TEXT}" if bad else HELP_TEXT
        replied, message = _reply(uid, group_id, content)
        if replied:
            _audit_command(uid, group_id, text)
        return replied, message

    logger.info("群内指令触发: user_id=%s group_id=%s range=%s~%s",
                uid, group_id, params["start"], params["end"])

    # 纯查询语义：只生成不落盘、不写推送状态、不推群。
    # 群内问「汇总 昨天」不该把昨天已归档的日报重写掉——定时任务推过的
    # 版本和现在生成的可能有差异（补录），而徽标还显示「已推送」，会对不上。
    content = f"【群内触发】\n{build_period_report(app, params['start'], params['end'])}"

    replied, message = _reply(uid, group_id, content)
    if replied:
        _audit_command(uid, group_id, text)
    return replied, message


def _parse_whitelist(raw: str) -> set[int]:
    """'10001, 10002' → {10001, 10002}；脏值直接丢掉。"""
    out: set[int] = set()
    for part in (raw or "").replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
        elif part:
            logger.warning("BOT_ADMIN_QQ_IDS 含非数字条目，已忽略: %r", part)
    return out
