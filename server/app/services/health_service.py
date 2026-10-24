"""系统探针取数：数据库连通性检查。

供 routes/health.py 使用。纯参数进、纯结果出——异常原样上抛，
由路由层决定日志措辞与响应码（探针失败不回细节给调用方）。
"""

from __future__ import annotations

from sqlalchemy import text

from ..db import db


def ping_database() -> None:
    """真连一次数据库（SELECT 1）。连不上抛异常，由调用方映射 503。"""
    db.session.execute(text("SELECT 1"))
