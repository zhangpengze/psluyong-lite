"""后台账号登录校验：口令比对与登录态落库。

纯参数进、纯结果出——session 写入、限流计数、审计留痕属于
请求流程，留在视图层组装；这里只管"这个账号能不能进"。
"""

from __future__ import annotations

from datetime import datetime

from ..db import db
from ..models import AdminUser


def authenticate(username: str, password: str) -> AdminUser | None:
    """校验后台账号口令。成功时顺手落 last_login_at 并提交。

    返回 None 表示拒绝——视图层统一渲染 401，不在此区分
    "账号不存在"与"密码错误"（对外也不该区分）。
    """
    user = AdminUser.query.filter_by(username=username, is_active=True).first()
    # 用户名不存在也走一次 hash 校验，避免响应时间泄露账号是否存在
    if user is None or not user.check_password(password):
        return None
    user.last_login_at = datetime.utcnow()
    db.session.commit()
    return user
