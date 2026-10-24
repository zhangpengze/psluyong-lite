"""对内网/本机地址发请求：一律绕开系统代理。

为什么专门写这一层：requests 默认读 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量。
公司电脑上装个 VPN 客户端、或者 IT 统一推了系统代理，这两个变量就会被设上——
于是发给"我们自己那台 192.168.x.x:8080"的请求会被送去代理，代理不认识这个地址，
回一个 502。

现象特别坑：**员工那边永远"连接失败"，运维在自己机器上（没配代理）怎么试都正常。**
2026-10-24 就是这个坑绊了一下：诊断包里的就绪探针在本机返回 502
`upstream connect failed`，而服务其实好好跑着。要不是当时多看了一眼
`status_code=502` 和 `raw` 是一段英文代理错误页，很容易误判成"服务端挂了"。

所以这里的姿态是：凡是我们自己发起的、目标是**我们自己的服务**的请求，一律直连。
外部 webhook（比如机器人推送）不在此列，那边该走代理就走代理。
"""

from __future__ import annotations

import requests

# requests 把 None 当作"这个 scheme 不走代理"，并且不会再从环境变量把代理补回来
# （merge_environment_settings 用的是 setdefault，键已存在就不覆盖）
def _no_proxy() -> dict[str, None]:
    return {"http": None, "https": None}


def get(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("proxies", _no_proxy())
    return requests.get(url, **kwargs)


def request(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("proxies", _no_proxy())
    return requests.request(method, url, **kwargs)
