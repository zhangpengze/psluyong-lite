"""官方 QQ 机器人错误码 → 能照着做的一句话。

单开一个文件的两个理由：
  1. 这张表会长（每个踩过的坑都会加一行），跟发送逻辑放一起会把发送器
     挤过 300 行门禁；
  2. 失败原因的措辞是**给人看的**，而发送逻辑是给机器跑的，改动机完全不同——
     运营随手加一条提示，不该碰到重试与鉴权代码。
"""

from __future__ import annotations

ERROR_HINTS: dict[int, str] = {
    40013: "access_token 无效或已过期，检查 QQ_BOT_APPID / QQ_BOT_SECRET 是否填对",
    40014: "appid 无效，检查 QQ_BOT_APPID",
    11244: "触发频率限制（群主动消息 60 条/分钟、1000 条/天/群），等一分钟再试",
    40034005: "msg_id 已过期（我们发的是主动消息，出现这个码说明被当成了被动回复）",
    40034102: (
        "机器人没有在群里主动发言的权限：手机 QQ → 该群 → 设置 → 机器人 → "
        "打开「机器人主动在群聊内发言」"
    ),
    40054002: (
        "group_openid 无效：要填群的 openid（群内 @机器人 消息事件里的 group_openid），"
        "不是群号"
    ),
    40054005: "msg_seq 重复",
}


def code_of(body: dict) -> int | None:
    """失败码在 err_code（新）或 code（旧）里；取不到返回 None 表示"没有失败码"。

    返回 -1 表示"有这个字段但读不懂"——按失败处理。宁可误报一次，
    也别把一次真拒发记成推送成功：那会让日志、徽标、仪表盘一起绿。
    """
    for key in ("err_code", "code"):
        if key in body:
            try:
                return int(body[key])
            except (TypeError, ValueError):
                return -1
    return None


def explain(code, message: str = "") -> str:
    """认不出来的码原样带上，绝不吞掉——未知错误码本身就是最有用的线索。"""
    try:
        num = int(code)
    except (TypeError, ValueError):
        raw = (message or "").strip()
        return raw or "未知错误"
    hint = ERROR_HINTS.get(num)
    if hint:
        return f"{hint}（err_code={num}）"
    raw = (message or "").strip()
    return f"err_code={num}" + (f"：{raw}" if raw else "")


__all__ = ["ERROR_HINTS", "code_of", "explain"]
