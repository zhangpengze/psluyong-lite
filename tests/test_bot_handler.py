"""QQ 机器人指令处理测试：不连真 NapCat，mock 推送与日报生成"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

import pytest
import requests

from bot import handler
from bot.report import PUSH_ATTEMPTS
from server.app.config import Config


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """每个用例清冷却 + 不配 webhook（除显式配置的用例外）

    默认给一套"能用"的授权：群号与 _event 默认值一致（888888），
    QQ 10001/10002 在白名单里 —— 否则 2026-10-24 收紧成 fail-closed 后，
    所有 happy-path 用例都会被门禁挡在业务逻辑之前。
    """
    handler.reset_cooldown()
    monkeypatch.setattr(Config, "NAPCAT_WEBHOOK", "")
    monkeypatch.setattr(Config, "REPORT_GROUP_ID", "888888")
    monkeypatch.setattr(Config, "BOT_ADMIN_QQ_IDS", "10001,10002")
    monkeypatch.setattr(Config, "BOT_ALLOWED_GROUP_IDS", "")
    monkeypatch.setattr(Config, "BOT_COOLDOWN_S", 10)
    monkeypatch.setattr(Config, "BOT_CALLBACK_SECRET", "")
    # 路由层默认放行"无密钥回调"，专门的安全用例会显式关掉
    monkeypatch.setattr(Config, "BOT_ALLOW_INSECURE", True)


def _event(text: str | None = None, *, user_id: int = 10001, group_id: int = 888888,
           segments=None, raw=None, message_type: str = "group",
           post_type: str = "message"):
    payload = {
        "post_type": post_type,
        "message_type": message_type,
        "sub_type": "normal",
        "message_id": 1,
        "group_id": group_id,
        "user_id": user_id,
        "self_id": 20001,
        "time": 1729500000,
    }
    if segments is not None:
        payload["message"] = segments
    elif raw is not None:
        payload["raw_message"] = raw
    elif text is not None:
        payload["message"] = [{"type": "text", "data": {"text": text}}]
    return payload


# ---------- 文本提取 ----------

def test_extract_text_from_segments():
    """结构化 message 数组：只取 text 段，at/face 天然被过滤"""
    payload = _event(segments=[
        {"type": "at", "data": {"qq": "20001"}},
        {"type": "text", "data": {"text": " 汇总 "}},
    ])
    assert handler.extract_text(payload) == "汇总"


def test_extract_text_strips_cq_codes():
    """raw_message 退化形态：剥掉 CQ 码，如 [CQ:at,qq=20001] 汇总"""
    payload = _event(raw="[CQ:at,qq=20001] 汇总")
    assert handler.extract_text(payload) == "汇总"


def test_extract_text_empty_when_no_text():
    assert handler.extract_text(_event(segments=[{"type": "image", "data": {"file": "x.jpg"}}])) == ""
    assert handler.extract_text({"message_type": "group"}) == ""


# ---------- 指令解析 ----------

@pytest.mark.parametrize("text,expected", [
    ("汇总", ("report", (None, None))),   # None = 今天，由测试填
    ("日报", ("report", (None, None))),
    ("数据汇总", ("report", (None, None))),
    ("帮助", ("help", None)),
    ("指令", ("help", None)),
    ("汇总一下", None),      # 非精确匹配，防止聊天里顺带触发
    ("汇总了", None),
    ("你好", None),
    ("", None),
])
def test_parse_command_shape(text, expected):
    result = handler.parse_command(text, today=date(2026, 10, 21))
    if expected is None:
        assert result is None
        return
    cmd, params = result
    exp_cmd, exp_range = expected
    assert cmd == exp_cmd
    if exp_cmd == "help":
        return
    exp_start, exp_end = exp_range
    assert params["start"] == (exp_start or date(2026, 10, 21))
    assert params["end"] == (exp_end or date(2026, 10, 21))


@pytest.mark.parametrize("arg,start,end", [
    ("", "2026-10-21", "2026-10-21"),           # 无参数 = 今天
    ("今天", "2026-10-21", "2026-10-21"),
    ("昨天", "2026-10-20", "2026-10-20"),
    ("前天", "2026-10-19", "2026-10-19"),
    ("10-20", "2026-10-20", "2026-10-20"),      # MM-DD 取今年
    ("2026-10-01", "2026-10-01", "2026-10-01"), # 全日期
    ("本周", "2026-10-19", "2026-10-21"),       # 10-21 是周三 → 周一 10-19 起
    ("本月", "2026-10-01", "2026-10-21"),
    ("上周", "2026-10-12", "2026-10-18"),
    ("01-05", "2026-01-05", "2026-01-05"),      # 未来日期回退到今年（而非明年）
])
def test_parse_range(arg, start, end):
    rng = handler.parse_range(arg, today=date(2026, 10, 21))
    assert rng == (date.fromisoformat(start), date.fromisoformat(end))


@pytest.mark.parametrize("arg", ["乱码", "汇总一下", "2026-13-01", "02-30", "10/20"])
def test_parse_range_rejects_garbage(arg):
    assert handler.parse_range(arg, today=date(2026, 10, 21)) is None


def test_parse_command_bad_date_falls_back_to_help():
    """「汇总 乱码」：指令名命中但日期无法识别 → 降级帮助 + 带上脏参数"""
    cmd, params = handler.parse_command("汇总 乱码", today=date(2026, 10, 21))
    assert cmd == "help"
    assert params["bad_arg"] == "乱码"


# ---------- 白名单与冷却 ----------

def _patch_pipeline(monkeypatch):
    """mock 掉日报生成和两个推送，返回调用记录"""
    calls = {"period": [], "user": [], "group": []}

    def fake_build_period_report(app, start, end):
        calls["period"].append((start, end))
        return "【区间汇总正文】"

    def fake_push_to_user(user_id, content):
        calls["user"].append((user_id, content))
        return True, "推送成功"

    def fake_push_to_group(content, group_id=None):
        calls["group"].append((content, group_id))
        return True, "推送成功"

    monkeypatch.setattr(handler, "build_period_report", fake_build_period_report)
    monkeypatch.setattr(handler, "push_to_user", fake_push_to_user)
    monkeypatch.setattr(handler, "push_to_group", fake_push_to_group)
    return calls


def _no_audit(monkeypatch):
    """默认不打审计，避免用例碰数据库；审计用例单独验"""
    monkeypatch.setattr(handler, "_audit_command", lambda *a, **k: None)


def test_handle_event_private_reply(app, monkeypatch):
    """核心链路：群里发「汇总」→ 私聊回触发者，不推群、不落盘"""
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)

    replied, message = handler.handle_event(app, _event("汇总"))

    assert replied is True
    assert "10001" in message
    # 查询语义：只生成，不 run_daily（不落盘/不写推送状态）
    assert calls["period"] == [(date.today(), date.today())]
    assert len(calls["user"]) == 1
    user_id, content = calls["user"][0]
    assert user_id == 10001
    assert "区间汇总正文" in content and "群内触发" in content
    assert calls["group"] == []  # 不推群


def test_handle_event_yesterday(app, monkeypatch):
    """「汇总 昨天」→ 单日查询，day 是昨天"""
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)

    replied, _ = handler.handle_event(app, _event("汇总 昨天"))

    assert replied is True
    assert calls["period"] == [(date.today() - timedelta(days=1), date.today() - timedelta(days=1))]


def test_handle_event_does_not_touch_archive(app, monkeypatch, tmp_path):
    """群内触发是纯查询：reports/ 目录不被创建、不写任何文件。

    落盘目录指到本用例自己的 tmp：共享 DATA_DIR/reports 里可能已经有别的
    用例（test_report / test_smoke 的 run_daily）写下的 .md，断言共享目录
    会随执行顺序变化——单跑绿、子集/随机顺序红就是这么来的。
    """
    import server.app.config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)

    handler.handle_event(app, _event("汇总"))

    reports_dir = tmp_path / "reports"
    assert not reports_dir.exists() or not list(reports_dir.glob("*.md"))


def test_handle_event_ignores_self_echo(app, monkeypatch):
    """机器人自己的消息回声要忽略（防自消费）"""
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)
    payload = _event("汇总")
    payload["self_id"] = payload["user_id"]

    replied, message = handler.handle_event(app, payload)

    assert (replied, message) == (False, "自身消息回声")
    assert calls["user"] == [] and calls["period"] == []


def test_handle_event_this_week(app, monkeypatch):
    """「汇总 本周」→ 区间汇总，走 build_period_report，不落单日状态"""
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)

    replied, message = handler.handle_event(app, _event("汇总 本周"))

    assert replied is True
    assert len(calls["period"]) == 1
    start, end = calls["period"][0]
    assert start == date.today() - timedelta(days=date.today().weekday())
    assert end == date.today()
    assert "区间汇总正文" in calls["user"][0][1]


def test_handle_event_help(app, monkeypatch):
    """「帮助」→ 回复指令列表，不生成日报"""
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)

    replied, _ = handler.handle_event(app, _event("帮助"))

    assert replied is True
    content = calls["user"][0][1]
    assert "汇总" in content and "本周" in content
    assert calls["period"] == []


def test_handle_event_help_on_bad_date_arg(app, monkeypatch):
    """「汇总 乱码」→ 回复帮助 + 指出脏参数"""
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)

    replied, _ = handler.handle_event(app, _event("汇总 乱码"))

    assert replied is True
    content = calls["user"][0][1]
    assert "无法识别日期参数「乱码」" in content
    assert calls["period"] == []


def test_handle_event_audits_command(app, monkeypatch):
    """成功触发写审计：actor=qq:号、target=群号、detail=原文"""
    _patch_pipeline(monkeypatch)
    audits = []
    monkeypatch.setattr(handler, "_audit_command",
                        lambda uid, gid, text: audits.append((uid, gid, text)))

    handler.handle_event(app, _event("汇总 昨天"))

    assert audits == [(10001, 888888, "汇总 昨天")]


def test_handle_event_no_audit_when_not_replied(app, monkeypatch):
    """白名单外/冷却期内不触发，也不写审计"""
    _patch_pipeline(monkeypatch)
    audits = []
    monkeypatch.setattr(handler, "_audit_command",
                        lambda uid, gid, text: audits.append((uid, gid, text)))

    handler.handle_event(app, _event("汇总"), whitelist={99999})

    assert audits == []


def test_handle_event_falls_back_to_triggering_group(app, monkeypatch):
    """私聊失败 → fallback 发回**触发所在的群**，不是日报群

    旧实现走 push_to_group(content)，用的是 REPORT_GROUP_ID：机器人同时待在
    A（办公群）和 B（日报群）时，A 群某人触发、私聊失败，整份报表（含员工
    姓名/稿件标题/失败明细）会被贴到 B 群——既泄数据又与设计矛盾。
    """
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)
    monkeypatch.setattr(Config, "BOT_ALLOWED_GROUP_IDS", "666666")
    monkeypatch.setattr(handler, "push_to_user", lambda uid, content: (False, "被拒收"))

    replied, message = handler.handle_event(app, _event("汇总", group_id=666666))

    assert replied is True
    assert "转推触发群" in message
    assert len(calls["group"]) == 1
    assert calls["group"][0][1] == 666666, "必须发回触发群，绝不能落到 REPORT_GROUP_ID"


def test_handle_event_ignores_non_command(app, monkeypatch):
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)
    replied, message = handler.handle_event(app, _event("大家辛苦啦"))
    assert (replied, message) == (False, "非指令消息")
    assert calls["period"] == []


def test_handle_event_ignores_non_group_message(app, monkeypatch):
    """私聊消息、通知事件全部静默过滤"""
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)
    replied, _ = handler.handle_event(app, _event("汇总", message_type="private"))
    assert replied is False
    replied, _ = handler.handle_event(app, _event("汇总", post_type="notice"))
    assert replied is False
    assert calls["period"] == []


def test_handle_event_whitelist(app, monkeypatch):
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)
    whitelist = {99999}

    replied, message = handler.handle_event(app, _event("汇总", user_id=10001), whitelist=whitelist)
    assert (replied, message) == (False, "用户不在白名单")
    assert calls["period"] == []

    replied, _ = handler.handle_event(app, _event("汇总", user_id=99999), whitelist=whitelist)
    assert replied is True


def test_handle_event_whitelist_empty_denies_everyone(app, monkeypatch):
    """白名单为空 = 谁都不能用（fail-closed）

    旧语义是"空=不限制"：机器人被拉进任何群，群里任何人都能触发，
    把含员工姓名/作者/标题/失败原因的报表私聊带走。2026-10-24 反转。
    """
    _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)

    replied, message = handler.handle_event(app, _event("汇总"), whitelist=set())

    assert (replied, message) == (False, "用户不在白名单")


def test_handle_event_group_not_allowed(app, monkeypatch):
    """群号不在允许列表里：即使人和指令都对，也不回"""
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)

    replied, message = handler.handle_event(app, _event("汇总", group_id=666666))

    assert (replied, message) == (False, "群不在允许列表")
    assert calls["period"] == []


def test_handle_event_group_whitelist_allows_extra_group(app, monkeypatch):
    """BOT_ALLOWED_GROUP_IDS 明确列出的群可以用（机器人常在日报群+管理群）"""
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)
    monkeypatch.setattr(Config, "BOT_ALLOWED_GROUP_IDS", "666666, 777777")

    replied, _ = handler.handle_event(app, _event("汇总", group_id=777777))
    assert replied is True
    assert len(calls["period"]) == 1

    replied, message = handler.handle_event(app, _event("汇总", group_id=888888))
    assert (replied, message) == (False, "群不在允许列表"), "列出来之后就只认列出的群"


def test_handle_event_cooldown(app, monkeypatch):
    """同一人冷却期内第二次触发直接忽略"""
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)
    assert handler.handle_event(app, _event("汇总"))[0] is True
    assert handler.handle_event(app, _event("汇总")) == (False, "冷却期内")
    assert len(calls["period"]) == 1
    # 换个人不受影响
    assert handler.handle_event(app, _event("汇总", user_id=10002))[0] is True


def test_handle_event_bad_user_id(app, monkeypatch):
    calls = _patch_pipeline(monkeypatch)
    _no_audit(monkeypatch)
    payload = _event("汇总")
    payload["user_id"] = None
    replied, message = handler.handle_event(app, payload)
    assert (replied, message) == (False, "user_id 异常")
    assert calls["period"] == []


# ---------- 路由端到端 ----------

def test_onebot_callback_end_to_end(client, app, monkeypatch):
    """POST 上报 → 200 → 后台线程完成私聊回复"""
    calls = _patch_pipeline(monkeypatch)
    monkeypatch.setattr("server.app.routes.bot._spawn",
                        lambda a, p: calls["user"].append(("spawned", p)))

    resp = client.post("/api/v1/bot/onebot", json=_event("汇总"))
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
    # _spawn 被同步化，验证它收到了原始 payload
    assert calls["user"][0][1]["user_id"] == 10001


def test_onebot_callback_rejects_bad_secret(client, monkeypatch):
    monkeypatch.setattr(Config, "BOT_CALLBACK_SECRET", "s3cret")
    resp = client.post("/api/v1/bot/onebot", json=_event("汇总"))
    assert resp.status_code == 403

    ok = client.post("/api/v1/bot/onebot", json=_event("汇总"),
                     headers={"X-Bot-Secret": "s3cret"})
    assert ok.status_code == 200

    bearer = client.post("/api/v1/bot/onebot", json=_event("汇总"),
                         headers={"Authorization": "Bearer s3cret"})
    assert bearer.status_code == 200


def test_onebot_callback_rejects_non_ascii_secret(client, monkeypatch):
    """密钥比较必须走 bytes：hmac.compare_digest 对含非 ASCII 的 str 会抛
    TypeError，攻击者发个非 ASCII 头就能把 403 变成 500。"""
    monkeypatch.setattr(Config, "BOT_CALLBACK_SECRET", "s3cret")
    resp = client.post("/api/v1/bot/onebot", json=_event("汇总"),
                       headers={"X-Bot-Secret": "sécret"})
    assert resp.status_code == 403


def test_onebot_callback_empty_secret_denied_by_default(client, monkeypatch):
    """没配 secret 时默认拒绝（fail-closed）

    旧实现留空即放行：任何能碰到 NAS 端口的人 POST 一条「汇总」就能拿到
    含员工姓名/作者/标题/失败原因的报表。要放行必须显式 BOT_ALLOW_INSECURE=1。
    """
    monkeypatch.setattr(Config, "BOT_CALLBACK_SECRET", "")
    monkeypatch.setattr(Config, "BOT_ALLOW_INSECURE", False)
    monkeypatch.setattr("server.app.routes.bot._spawn", lambda a, p: pytest.fail("不该被触发"))

    resp = client.post("/api/v1/bot/onebot", json=_event("汇总"))

    assert resp.status_code == 403


def test_onebot_callback_insecure_opt_in_allowed(client, monkeypatch):
    """显式开逃生阀（完全隔离内网的调试场景）时才放行无密钥回调"""
    monkeypatch.setattr(Config, "BOT_CALLBACK_SECRET", "")
    monkeypatch.setattr(Config, "BOT_ALLOW_INSECURE", True)
    monkeypatch.setattr("server.app.routes.bot._spawn", lambda a, p: None)

    resp = client.post("/api/v1/bot/onebot", json=_event("汇总"))

    assert resp.status_code == 200


def test_onebot_callback_filters_before_spawning(client, monkeypatch):
    """心跳/通知/私聊不进线程：NapCat 上报量里这类占大头"""
    monkeypatch.setattr("server.app.routes.bot._spawn", lambda a, p: pytest.fail("非群消息不该起线程"))

    heartbeat = client.post("/api/v1/bot/onebot", json={"post_type": "meta_event", "meta_event_type": "heartbeat"})
    assert heartbeat.status_code == 200
    assert heartbeat.get_json()["handled"] is False

    private = client.post("/api/v1/bot/onebot", json=_event("汇总", message_type="private"))
    assert private.status_code == 200
    assert private.get_json()["handled"] is False


def test_onebot_callback_rate_limited(client, monkeypatch):
    """回调也要限流：一个死循环脚本就能把线程刷干"""
    monkeypatch.setattr("server.app.routes.bot._spawn", lambda a, p: None)
    from server.app import ratelimit
    from server.app.routes import bot as bot_route

    ratelimit.reset("bot:127.0.0.1")
    monkeypatch.setattr(bot_route, "BOT_RATE_LIMIT", 2)

    codes = [client.post("/api/v1/bot/onebot", json=_event("汇总")).status_code for _ in range(3)]

    assert codes == [200, 200, 429]
    ratelimit.reset("bot:127.0.0.1")


def test_onebot_callback_swallows_empty_body(client, monkeypatch):
    monkeypatch.setattr("server.app.routes.bot._spawn",
                        lambda a, p: pytest.fail("空体不应触发处理"))
    resp = client.post("/api/v1/bot/onebot", json="not-a-dict")
    assert resp.status_code == 200
    assert resp.get_json()["handled"] is False


def test_async_handler_actually_runs_thread(client, app, monkeypatch):
    """不 patch _spawn：真实线程跑完 push_to_user，且审计真的落库"""
    calls = _patch_pipeline(monkeypatch)
    resp = client.post("/api/v1/bot/onebot", json=_event("汇总 昨天"))
    assert resp.status_code == 200

    for _ in range(50):
        if calls["user"]:
            break
        time.sleep(0.05)
    assert len(calls["user"]) == 1

    # 审计落库：路由线程包了 app_context，audit.py 对无请求上下文容错
    with app.app_context():
        from server.app.models import AuditLog

        entry = AuditLog.query.filter_by(action="bot_command").order_by(AuditLog.id.desc()).first()
    assert entry is not None, "群内指令没有写审计"
    assert entry.actor == "qq:10001"
    assert entry.target == "group:888888"
    assert entry.detail == "汇总 昨天"
