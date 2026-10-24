"""上传闸门装饰器：在**读请求体之前**挡掉过量上传

为什么不写在视图函数体里：`request.form` 一被访问就把整个 multipart 读完
（最大 64MB）。写在视图体里的第一行虽然也能挡住后续逻辑，但"第一行"是个
口头约定——以后有人挪一行代码就破了。做成装饰器，顺序是结构性的：装饰器
先跑，视图体根本没进。

429 而不是 4xx 里的其它码：sidecar 的补传队列只把 400/409/422 当"业务拒绝"
删掉，429 会留在队列里等下一轮（`retryable: true` 是给老版本客户端看的），
否则被限流一次就等于丢一条记录。
"""

from __future__ import annotations

import logging
from functools import wraps

from flask import jsonify, request

from . import ratelimit

logger = logging.getLogger(__name__)


def upload_guard(view):
    """给"能传大 body"的写接口加每来源 + 全站两道限流"""

    @wraps(view)
    def wrapper(*args, **kwargs):
        allowed, retry_after = ratelimit.upload_allowed(request.remote_addr)
        if not allowed:
            logger.warning(
                "上传过于频繁（每来源 %s 次/%ss，全站 %s 次）: remote=%s path=%s",
                ratelimit.UPLOAD_PER_SOURCE,
                ratelimit.UPLOAD_WINDOW_S,
                ratelimit.UPLOAD_GLOBAL,
                request.remote_addr,
                request.path,
            )
            return (
                jsonify(
                    {"ok": False, "error": "上传过于频繁，请稍后再试", "retryable": True}
                ),
                429,
                {"Retry-After": str(retry_after)},
            )
        return view(*args, **kwargs)

    return wrapper
