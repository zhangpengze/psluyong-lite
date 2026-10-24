"""测试连接：用**表单里填的**地址和 Key 真发一次请求。

原来的"测试连接"按钮调的是 `/status`，而 `/status` 用的是磁盘上的旧配置——
员工改了地址点测试，toast 打印的是新地址，实际测的却是老地址：提示"连接成功"，
一保存就连不上。这类"测了个假的"比不给按钮更糟，因为它把排查方向带偏了。

四种结论分开报，员工才能自己动手：

    invalid      地址格式就不对（少了 http://）
    unreachable  地址连得上但没响应（网线/端口/服务没起）
    key_bad      地址通了、Key 不对（401/403）或服务端版本太老（404 没有该接口）
    ok           地址通 + Key 有效

为什么不只看 /health：它不需要鉴权。地址填对了但 Key 是上一任的，/health 照样 200，
员工会以为万事大吉，然后第一条记录 401。
"""

from __future__ import annotations

import logging

import requests

from . import net

logger = logging.getLogger(__name__)

TEST_TIMEOUT_S = 4
# 拿"有没有权限"最便宜的鉴权接口：只取 1 条记录
AUTH_PROBE_PATH = "/api/v1/records?size=1"


def _result(level: str, message: str, **extra) -> dict:
    return {
        "level": level,
        "message": message,
        "server_reachable": level in ("key_bad", "server_error", "ok"),
        "key_ok": level == "ok",
        **extra,
    }


def probe(server_url: str, api_key: str, timeout: float = TEST_TIMEOUT_S) -> dict:
    """返回值永远不抛异常——这是给按钮用的，任何情况都要有话说。"""
    url = (server_url or "").strip().rstrip("/")
    if not url:
        return _result("invalid", "请先填写服务器地址，例如 http://192.168.1.10:8080")
    if not url.lower().startswith(("http://", "https://")):
        return _result("invalid", "服务器地址要以 http:// 或 https:// 开头")

    try:
        net.get(f"{url}/health", timeout=timeout)
    except requests.exceptions.SSLError as e:
        logger.info("测试连接 SSL 失败 %s: %s", url, e)
        return _result("unreachable", f"HTTPS 证书有问题：{e}")
    except requests.RequestException as e:
        logger.info("测试连接不可达 %s: %s", url, e)
        return _result("unreachable", f"连不上 {url}：请检查地址、端口和网线（{type(e).__name__}）")
    except Exception as e:  # noqa: BLE001 - 按钮不许抛异常，任何意外都要变成一句话
        logger.exception("测试连接出现意外错误 %s", url)
        return _result("unreachable", f"测试连接出错（{type(e).__name__}: {e}）")

    try:
        auth = net.get(
            f"{url}{AUTH_PROBE_PATH}",
            headers={"X-API-Key": (api_key or "").strip()},
            timeout=timeout,
        )
    except requests.RequestException as e:
        logger.info("测试连接鉴权请求失败 %s: %s", url, e)
        return _result("unreachable", f"服务器连上后没有响应：{type(e).__name__}")
    except Exception as e:  # noqa: BLE001
        logger.exception("测试连接鉴权请求意外错误 %s", url)
        return _result("unreachable", f"测试连接出错（{type(e).__name__}: {e}）")

    code = auth.status_code
    if code in (401, 403):
        return _result("key_bad", "地址对了，但 API Key 无效——请向主管重新索取", status=code)
    if code == 404:
        return _result(
            "key_bad",
            "地址通了，但服务器上没有这个接口——多半是服务端版本太旧或是别的系统",
            status=code,
        )
    if code >= 400:
        return _result("server_error", f"服务器返回 {code}，请把这个数字告诉运维", status=code)
    return _result("ok", f"连接成功（{url}）", status=code)
