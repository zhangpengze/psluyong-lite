"""硬超时：没人看着的时候也必须把任务收掉

窗口关了 / 前端崩了 / 员工走开，这时没有任何人调 `/status`，也没有人点取消。
旧实现里 busy 锁就一直被握着，之后每一次生成都撞 409「上一次还没结束」——
软件看着在跑，实际什么都不干，员工只能重启。

判据与终态文案放在这里，是因为轮询路径（status）和后台 reaper（_sweep_once）
必须用同一套：两处各写一份，早晚有一处先漂。
"""

from __future__ import annotations

# 正常 5~20 秒，冷启预算 60 秒，界面 90 秒就给员工「取消生成」了。
# 300 秒是"没人看着"时的最后一道（窗口关了 / 前端死了 / 员工走开）。
HARD_TIMEOUT_S = 300.0


def should_abort(elapsed_s: float, limit_s: float = HARD_TIMEOUT_S) -> bool:
    """跑这么久还没终态就该放弃了。

    **只看总耗时，不看心跳**：worker 卡在 COM 调用里时 pywin32 会释放 GIL，
    心跳线程照常每 2 秒 touch，看起来一切正常——等心跳变旧，说明 worker 主线程
    已经把 GIL 占死了，而那种情况进程很快会被 OS 收走（走 uncertain 分支）。
    """
    return elapsed_s >= limit_s


def timeout_final(elapsed_ms: int, limit_s: float = HARD_TIMEOUT_S) -> dict:
    """超时终态。success 一律 False：图有可能已经导出来了，只是 PS 卡在收尾，
    所以文案必须让员工自己去输出目录看一眼，绝不能报成功。"""
    minutes = int(round(limit_s / 60))
    return {
        "state": "timeout",
        "success": False,
        "image_path": "",
        "message": (
            f"等了 {minutes} 分钟还没结束，已中止（Photoshop 可能卡在对话框或大模板上）。"
            "请到输出目录确认这次有没有生成图片，再决定要不要重试。"
        ),
        "warnings": [
            "生成被强制中止：Photoshop 里可能留下这个模板的未保存文档，"
            "关掉它再重试，否则文档会越叠越多、最终报暂存盘不足。"
        ],
        "duration_ms": elapsed_ms,
    }
