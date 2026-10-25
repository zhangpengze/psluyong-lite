"""官方机器人发送器：鉴权 / 判成功 / 重试 / 目标 / 配置缺失。

这一层最容易出的是**假成功**：HTTP 200 但平台拒发（err_code 40034102），
于是日志绿、徽标绿、仪表盘绿，群里一条消息都没有，而"定时日报未推送成功"
这条兜底告警的判据正是 pushed 为假——整套告警一起失灵。
所以判成功的用例必须同时覆盖"200 且没 err_code"和"200 但有 err_code"。
"""

from __future__ import annotations

import importlib
import time

import pytest
import requests

from bot import qqbot_sender as sender
from bot.report_render import MESSAGE_MAX_BYTES
from server.app.config import Config


# ---------- 鉴权 ----------

def test_鉴权头是_QQBot_前缀不是_Bearer(qqbot):
    """写成 Bearer 一律 401，而且 401 的报文里看不出是前缀写错了"""
    sender.push_to_group("内容")

    assert qqbot.headers[0]["Authorization"] == "QQBot test-token"
    assert "Bearer" not in qqbot.headers[0]["Authorization"]


def test_token_在两次发送之间复用(qqbot):
    """token 两小时有效，每次推送都去换既浪费又会被限流"""
    sender.push_to_group("第一条")
    sender.push_to_group("第二条")

    assert len(qqbot.sent) == 2
    assert qqbot.token_calls == 1


def test_token_提前60秒刷新而不卡到期点(qqbot):
    """expires_in 是**字符串** "7200"，且缓存要提前 60 秒失效：
    卡着过期点换 token 等于赌最后一秒的网络没问题，而那一刻恰好最忙（18:00）。

    断言必须卡到"少掉的那 60 秒"上：只写 `<= 7200` 的话，把提前量去掉照样绿
    （变异验证时抓到过一次——真掉进去过这个坑）。
    """
    token, err = sender.get_access_token()

    assert token and not err
    remain = sender._token_cache["expires_at"] - time.monotonic()
    expected = 7200 - sender.TOKEN_REFRESH_MARGIN_S
    assert expected - 30 <= remain <= expected + 5, (
        f"缓存有效期 {remain:.0f}s，应该是 {expected}s（7200 - 提前量）；"
        "少掉的那段就是提前刷新窗口"
    )


def test_缓存到期才重新取_token(qqbot):
    sender.get_access_token()
    sender._token_cache["expires_at"] = time.monotonic() + 30  # 还没到期
    sender.get_access_token()
    assert qqbot.token_calls == 1

    sender._token_cache["expires_at"] = time.monotonic() - 1  # 到期
    sender.get_access_token()
    assert qqbot.token_calls == 2


def test_取_token_失败要报清楚而不是静默(qqbot):
    for _ in range(sender.PUSH_ATTEMPTS):  # 三次都取不到
        qqbot.refuse_token()

    ok, msg = sender.push_to_group("内容")

    assert ok is False
    assert "access_token" in msg and "40013" in msg
    assert qqbot.send_attempts == 0, "拿不到 token 就不该去发消息"
    assert qqbot.token_calls == sender.PUSH_ATTEMPTS, "取 token 失败也要走满重试"


def test_取_token_偶发失败后重试能发出去(qqbot):
    """一次取不到不等于今天没日报：重试一次拿到 token 就该正常发"""
    qqbot.refuse_token()

    ok, _ = sender.push_to_group("内容")

    assert ok is True
    assert qqbot.token_calls == 2 and qqbot.send_attempts == 1


def test_401_会丢掉旧_token_再试一次(qqbot):
    """平台侧提前轮换/刚改了 AppSecret：不换 token 的话后面永远 401"""
    qqbot.refuse_http(401)

    ok, _ = sender.push_to_group("内容")

    assert ok is True
    assert qqbot.token_calls == 2, "401 之后必须重新取 token 再发"


# ---------- 判成功（本节的重点：HTTP 200 也可能是失败） ----------

def test_200_且无_err_code_才算成功(qqbot):
    ok, msg = sender.push_to_group("内容")

    assert ok is True and msg == "推送成功"
    assert qqbot.sent[0] == {"content": "内容", "msg_type": 0}


def test_200_但_body_带_err_code_必须判失败(qqbot):
    """群没开「机器人主动在群聊内发言」时就是 200 + 40034102。
    只看状态码 → pushed 记成 True → "定时日报未推送成功"永远不会触发。"""
    qqbot.refuse(40034102, "主动消息失败, 无权限")

    ok, msg = sender.push_to_group("内容")

    assert ok is False, "HTTP 200 但平台拒发，不能报成功"
    assert "机器人主动在群聊内发言" in msg, "错误码要翻成能照着做的一句话"
    assert "40034102" in msg
    assert qqbot.send_attempts == 1, "业务拒绝重发也一样，不该重试"


def test_旧版_code_字段也认(qqbot):
    """平台部分应答里失败码在 code 里（不是 err_code），漏了就会假成功"""
    qqbot.refuse_legacy(40054002, "群不存在")

    ok, msg = sender.push_to_group("内容")

    assert ok is False
    assert "40054002" in msg and "group_openid" in msg


def test_非_JSON_的_200_不误判为失败(qqbot):
    """反代错误页/空 body 不等于推送失败：宁可放过，也不要半夜被假警报叫起来"""
    qqbot.reply_html()

    ok, _ = sender.push_to_group("内容")
    assert ok is True


def test_频率限制要翻成可执行的提示(qqbot):
    qqbot.refuse(11244, "frequency limit")

    ok, msg = sender.push_to_group("内容")

    assert ok is False
    assert "频率限制" in msg and "1000" in msg
    assert qqbot.send_attempts == 1, "限频重试只会更糟，5 秒退避救不了 60 秒窗口"


# ---------- 重试边界 ----------

def test_5xx_退避重试后成功(qqbot):
    qqbot.refuse_http(500)
    qqbot.refuse_http(502)

    ok, _ = sender.push_to_group("内容")

    assert ok is True
    assert qqbot.send_attempts == 3


def test_连不上重试满三次后报失败(qqbot):
    qqbot.send_raise = requests.RequestException("connection refused")

    ok, msg = sender.push_to_group("内容")

    assert ok is False
    assert qqbot.send_attempts == sender.PUSH_ATTEMPTS == 3
    assert "已重试 3 次" in msg


def test_4xx_不重试(qqbot):
    """群 openid 写错/机器人被移出群：重发同样失败，重试只让人晚 20 秒才知道"""
    qqbot.refuse_http(404, "not found")

    ok, msg = sender.push_to_group("内容")

    assert ok is False
    assert qqbot.send_attempts == 1
    assert "不重试" in msg


# ---------- 目标与配置 ----------

def test_发到_group_openid_而不是群号(qqbot):
    sender.push_to_group("内容")

    assert qqbot.urls[-1] == "https://api.bot.qq.test/v2/groups/GROUP_OPENID_TEST/messages"


def test_指定_openid_时不再要求默认群(qqbot, monkeypatch):
    """显式指定目标时不该因为没配默认群而拒绝——否则永远发不出去"""
    monkeypatch.setattr(Config, "QQ_BOT_GROUP_OPENID", "")

    ok, _ = sender.push_to_group("内容", group_id="OTHER_OPENID")

    assert ok is True
    assert qqbot.urls[-1].endswith("/v2/groups/OTHER_OPENID/messages")


def test_没配机器人时跳过且一次请求都不发(qqbot, monkeypatch):
    monkeypatch.setattr(Config, "QQ_BOT_APPID", "")

    ok, msg = sender.push_to_group("内容")

    assert ok is False
    assert "QQ_BOT_APPID" in msg and "跳过" in msg
    assert qqbot.urls == [], "没配置就不该发任何请求"


def test_配了机器人但没配群_openid(qqbot, monkeypatch):
    monkeypatch.setattr(Config, "QQ_BOT_GROUP_OPENID", "")

    ok, msg = sender.push_to_group("内容")

    assert ok is False
    assert "QQ_BOT_GROUP_OPENID" in msg
    assert qqbot.urls == []


def test_域名可配_默认走官方(qqbot, monkeypatch):
    monkeypatch.setattr(Config, "QQ_BOT_API_BASE", "")
    assert sender.api_base() == sender.DEFAULT_API_BASE == "https://api.bot.qq.com"


# ---------- 内容截断 ----------

def test_超长内容发送前就截断(qqbot):
    """整条超长会被平台拒收，群里没消息——所以截的是发送副本"""
    long_text = "\n".join(f"  · 合作伙伴{i}：模板{i}：{i} 条" for i in range(300))

    ok, _ = sender.push_to_group(long_text)

    assert ok is True
    sent = qqbot.texts[0]
    assert len(sent.encode("utf-8")) <= MESSAGE_MAX_BYTES + 200
    assert "已省略" in sent and "完整版见后台" in sent


# ---------- 错误码翻译 ----------

def test_错误码翻成能照着做的一句话():
    from bot.qqbot_errors import explain

    msg = explain(40034102)
    assert "机器人主动在群聊内发言" in msg and "40034102" in msg

    msg = explain(40054002)
    assert "不是群号" in msg, "最常见的手误就是把群号当 openid 填"


def test_认不出的错误码原样带上而不是吞掉():
    """未知码本身就是最有用的线索：翻不出来就照抄，绝不能变成"推送失败"三个字"""
    from bot.qqbot_errors import explain

    assert "99999" in explain(99999, "something broke")
    assert "something broke" in explain(99999, "something broke")
    assert explain(None, "只有一句话") == "只有一句话"


def test_错误码字段读不懂时按失败处理():
    """有字段但值不是数字：宁可误报一次，也别把真拒发记成成功"""
    from bot.qqbot_errors import code_of

    assert code_of({"err_code": "abc"}) == -1
    assert code_of({"err_code": 0}) == 0
    assert code_of({"code": 40054002}) == 40054002
    assert code_of({"id": "MSG-1"}) is None, "成功应答里没有失败码"


# ---------- 删干净的反向守卫 ----------
# 用户裁决：删掉"登录真 QQ 号当机器人"那条路（NapCat）。这几条守着它别被带回来。

def test_onebot_那套代码已经删掉():
    import bot

    assert not hasattr(bot, "handle_event"), "群内指令（handle_event）已随 NapCat 一起删除"
    for name in ("bot.handler", "bot.access", "server.app.routes.bot"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


def test_配置里不再有_napcat_相关项():
    for name in ("NAPCAT_WEBHOOK", "NAPCAT_TOKEN", "REPORT_GROUP_ID", "BOT_CALLBACK_SECRET"):
        assert not hasattr(Config, name), f"{name} 应随 NapCat 一起删掉"
    for name in ("QQ_BOT_APPID", "QQ_BOT_SECRET", "QQ_BOT_GROUP_OPENID", "QQ_BOT_API_BASE"):
        assert hasattr(Config, name), f"缺少 {name}"


def test_report_不许再留一份重试常量():
    """重试常量只能有一份（在 qqbot_sender）。留副本的后果是测试里 patch
    bot.report.PUSH_BACKOFF_S 看着生效、其实那个模块根本不读它。"""
    from bot import report as report_mod

    assert not hasattr(report_mod, "PUSH_BACKOFF_S")
    assert not hasattr(report_mod, "PUSH_ATTEMPTS")
