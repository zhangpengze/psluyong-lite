"""轻量启动迁移

为什么不用 create_all 就够了：create_all 只建"缺失的表"，表已存在就
整个跳过——以后给 Record 加字段、加索引，NAS 上的老库永远不会跟着变，
新代码一查就报"没有这个列"。

这里做最小可行迁移（不引入 alembic）：
    1. 对每张已存在的表，对比模型列与 PRAGMA table_info，缺的列用
       ALTER TABLE ADD COLUMN 补上（新增列按可空处理，不写死默认值，
       避免老行回填的历史包袱）
    2. 补建缺失的索引（包括部分唯一索引）
改列类型/删列/重命名这类破坏性操作仍然走 server/backup.py 恢复流程，
这里刻意不碰——自动迁移做破坏性变更是事故源头。
"""

from __future__ import annotations

import logging

from flask import Flask
from sqlalchemy import inspect, text

from .db import db

logger = logging.getLogger(__name__)

# 已经废弃、必须从老库里删掉的索引。
#
# create_all 只建"缺失的"，不会删多余的；模型里改了索引名，老库会同时
# 保留旧索引——旧规则继续拦数据，表现为"代码明明改了，线上还是 409"。
# 只删我们自己命名的索引，SQLite 自动索引（sqlite_autoindex_*）不碰。
_DEPRECATED_INDEXES: dict[str, tuple[str, ...]] = {
    # 去重键三次调整：(work_date, author, title)
    #   → (work_date, employee_name, author, title)
    #   → (work_date, employee_name, template_name, author, title)
    #   → 同上但只约束 status='success'（失败记录不再挡住重试）
    # 老库这些历史索引都要删，否则旧规则继续拦数据。
    "records": (
        "uq_records_date_author_title_active",
        "uq_records_date_employee_author_title_active",
        "uq_records_dedup_active",
    ),
}


def run_migrations() -> list[str]:
    """补列 + 补索引 + 删废弃索引，返回执行过的变更清单（供测试与日志）"""
    db.create_all()  # 先建缺失的表，已有表保持原样

    engine = db.engine
    inspector = inspect(engine)
    applied: list[str] = []

    for table in db.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue  # create_all 刚建好的，结构必然是新的

        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        with engine.begin() as conn:
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                col_type = col.type.compile(engine.dialect)
                # SQLite 的 ADD COLUMN 不支持在加列的同时回填非空默认值，
                # 所以新列一律按可空加——历史行为由模型层 default 保证
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'))
                applied.append(f"{table.name}.{col.name}")
                logger.warning("老库补列 %s.%s（代码比库新，属正常升级）", table.name, col.name)

        existing_indexes = {i["name"] for i in inspector.get_indexes(table.name)}

        for name in _DEPRECATED_INDEXES.get(table.name, ()):
            if name not in existing_indexes:
                continue
            with engine.begin() as conn:
                conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
            existing_indexes.discard(name)
            applied.append(f"drop-index:{name}")
            logger.warning("老库删除废弃索引 %s（去重规则已变更）", name)

        with engine.begin() as conn:
            for idx in table.indexes:
                if idx.name in existing_indexes:
                    continue
                idx.create(engine)
                applied.append(f"index:{idx.name}")
                logger.warning("老库补索引 %s", idx.name)

    return applied


def init_database(app: Flask) -> None:
    """create_app 时的建库入口：建表 + 迁移 + 种子数据"""
    from .models import init_seed

    db.create_all()
    run_migrations()
    init_seed(app)
