"""Photoshop 连接的重试预算与错误分类（从 ps_connector 拆出，独立可测）。

为什么不是「重试 3 次、每次等 2 秒」：

    `Dispatch("Photoshop.Application")` 在 PS 没运行时**不是立刻失败，而是静默把
    PS 拉起来**，冷启实测 15~40 秒。旧实现总共约 6 秒预算，于是员工装好软件后
    第一次点「生成录用图」必然失败，还会入库一条 failed 并触发失败告警——在员工
    看来就是"这软件是坏的"，第二次再点又好了。这是最难解释的一类故障。

现在的做法：

  1. 先判断 PS 在不在跑 —— 用它决定预算长短，而不是拍脑袋定重试次数
  2. PS 没跑、又知道 exe 路径时，**先把进程拉起来**再等 COM 就绪
     （比靠 Dispatch 隐式拉起快，也避免 Dispatch 卡死时毫无动静）
  3. 按截止时刻退避重试：冷启动 60 秒、已运行 10 秒
  4. 失败时给的是「PS 正在启动，等界面出来再点一次」这种能执行的话

所有函数纯逻辑或纯探测，不碰 PSService 状态，方便单测与变异验证。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 冷启动预算：留够 40 秒实测上限 + 老机器余量。第一个吃螃蟹的员工不该被罚一次失败
COLD_START_BUDGET_S = 60.0
# PS 已经在跑还连不上，说明是 COM 层的问题，等这么久够了——再等只是让员工干瞪眼
WARM_BUDGET_S = 10.0
# 硬上限：sleep 被替换成空实现（测试）时靠它兜住，避免按截止时刻空转
MAX_ATTEMPTS = 40
_FIRST_DELAY_S = 0.5
_MAX_DELAY_S = 2.5

RETRY = "retry"
FATAL = "fatal"
UNKNOWN = "unknown"

# 可以再等一会儿的错误：服务端还没起、调用被拒、对象还没注册进 ROT
_RETRY_CODES = frozenset(
    {
        0x80080005,  # CO_E_SERVER_EXEC_FAILURE 服务端进程起不来（PS 正在启动）
        0x800706BE,  # RPC_S_CALL_FAILED 冷启动期间的远程调用失败
        0x8001010A,  # RPC_E_SERVERCALL_RETRYLATER 服务器忙
        0x80010001,  # RPC_E_CALL_REJECTED 调用被拒（PS 正在初始化）
        0x800401E3,  # MK_E_UNAVAILABLE 运行对象表里还没有它
        0x80080008,  # CO_E_SERVER_STOPPING 正在启动/退出过程中
    }
)

# 再试也没用的错误：没装、没注册、路径错——直接把根因翻译给人看
#
# 0x800706BA（RPC_S_SERVER_UNAVAILABLE）故意留在致命类而不重试：连接阶段撞上它
# 八成是 PS 已经崩了或 COM 代理卡死，等 10 秒不如直接告诉员工"重新打开 PS"。
_FATAL_MESSAGES = {
    0x80040154: (
        "Photoshop COM 组件未注册。\n"
        "请确认已安装 Photoshop（不是仅安装 Creative Cloud 客户端），"
        "或尝试修复安装后重启电脑。"
    ),
    0x800706BA: "Photoshop 连接已断开（可能已崩溃），请重新打开 Photoshop 后再试。",
    0x80070002: "找不到 Photoshop 程序文件，请检查安装是否完整，或重新安装。",
    0x8007007E: "Photoshop 相关组件缺失（可能是破解版/绿色版），请更换为官方安装版。",
}


def error_code(exc: BaseException) -> int | None:
    """取 COM 错误的 HRESULT，统一成无符号 32 位（0x8007xxxx 这种写法）。

    `excepinfo` 属性存在但可以是 None，直接下标会抛 TypeError 从重试循环里漏出——
    这里统一收敛：取不到就是 None，由分类逻辑当作"不认识"。

    归一化是必须的：pywin32 给的 hresult 是**有符号**负数（-2147023174），
    而所有人写错误码都写 0x800706BA。不 mask 成无符号，所有码表都命中不了，
    "PS 正在启动"会被当成未知错误立刻抛出——测试里正是这么暴露出来的。
    """
    code = getattr(exc, "hresult", None)
    if not code:
        excepinfo = getattr(exc, "excepinfo", None)
        try:
            code = (excepinfo or (None,))[0]
        except (TypeError, IndexError):
            return None
    if not code:
        return None
    return int(code) & 0xFFFFFFFF


def classify(exc: BaseException) -> str:
    """retry / fatal / unknown。

    只认 COM 错误对象：普通 Python 异常原样当 unknown，由调用方抛出——
    把 ValueError 当成"PS 在启动"重试 60 秒，只会把真正的 bug 藏起来。
    """
    code = error_code(exc)
    if code is None:
        return UNKNOWN
    if code in _FATAL_MESSAGES:
        return FATAL
    if code in _RETRY_CODES:
        return RETRY
    return UNKNOWN


def fatal_message(exc: BaseException) -> str:
    """致命错误的可执行文案；不认识的错误返回空串（调用方自己抛原始异常）。"""
    code = error_code(exc)
    return _FATAL_MESSAGES.get(code, "") if code is not None else ""


def budget_for(cold_start: bool) -> float:
    return COLD_START_BUDGET_S if cold_start else WARM_BUDGET_S


def retry_delay(attempt: int) -> float:
    """0.5 / 1 / 2 / 2.5 / 2.5…：冷启动前期变化快，后期不必再加密。"""
    delay = _FIRST_DELAY_S * (2 ** max(0, attempt))
    return min(_MAX_DELAY_S, delay)


def _tasklist_has_photoshop() -> bool | None:
    """Windows 任务列表里有没有 PS。返回 None 表示这条途径不可用。"""
    if sys.platform != "win32":
        return None
    try:
        proc = subprocess.run(
            ["tasklist", "/NH", "/FI", "IMAGENAME eq Photoshop.exe"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("tasklist 查询失败，改用 COM 判断: %s", e)
        return None
    return "photoshop" in (proc.stdout or "").lower()


def _com_object_active(win32com) -> bool:
    """运行对象表里有没有活的 PS。GetActiveObject 不会拉起新进程。"""
    if win32com is None:
        return False
    try:
        win32com.client.GetActiveObject("Photoshop.Application")
        return True
    except Exception:  # noqa: BLE001 - 任何失败都只是"没找到"，不是错误
        return False


def is_photoshop_running(win32com=None) -> bool:
    """PS 进程在不在。

    先问操作系统（明确答案），再用 COM 运行对象表兜底（tasklist 被安全软件拦时）。
    都问不出来时返回 False —— 宁可给它长一点的预算，也不要让第一次生成必然失败。
    """
    running = _tasklist_has_photoshop()
    if running is not None:
        return running
    return _com_object_active(win32com)


def prelaunch(photoshop_exe: str | Path) -> bool:
    """按配置的路径先把 PS 拉起来，失败不影响后续（Dispatch 还会自己试一次）。

    这就是 `ps_path` 这个配置项存在的意义：以前它被赋值后从未被使用，
    员工填了路径也没用；而靠 COM 隐式拉起 PS 在部分机器上会直接返回失败。
    """
    exe = str(photoshop_exe or "").strip()
    if not exe:
        return False
    path = Path(exe)
    try:
        if not path.is_file():
            logger.warning("配置的 Photoshop 路径不存在：%s（改为让 COM 自己拉起）", path)
            return False
        subprocess.Popen(
            [str(path)],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logger.info("正在启动 Photoshop：%s", path)
        return True
    except OSError as e:
        logger.warning("启动 Photoshop 失败（%s）：%s", path, e)
        return False
