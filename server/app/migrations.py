"""轻量启动迁移

为什么不用 create_all 就够了：create_all 只建"缺失的表"，表已存在就
整个跳过——以后给 Record 加字段、加索引，NAS 上的老库永远不会跟着变，
新代码一查就报"没有这个列"。

这里做最小可行迁移（不引入 alembic）：
    1. 对每张已存在的表，对比模型列与 PRAGMA table_info，缺的列用
       ALTER TABLE ADD COLUMN 补上（新增列按可空处理——SQLite 的
       ADD COLUMN 不允许同时声明非空默认值），随后若模型定义了
       default，就再跟一条 UPDATE 把存量行回填成默认值而不是留 NULL；
    2. 补建缺失的索引（包括部分唯一索引）；
改列类型/删列/重命名这类破坏性操作仍然走 python -m server.backup 恢复流程，
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


# 「模型没给可用默认值」的标记：template_name 这类无 default 的列，
# 补列后宁可留 NULL 也不能凭空编一个值出来。
_NO_DEFAULT = object()


def _scalar_default(col) -> object:
    """模型为列定义的标量默认值（callable 会当场求值），没有则返回 _NO_DEFAULT"""
    default = col.default
    if default is None:
        return _NO_DEFAULT
    if default.is_scalar:
        return default.arg
    if default.is_callable:
        return default.arg()
    return _NO_DEFAULT


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
                # SQLite 的 ADD COLUMN 不允许同时声明非空默认值，所以新列一律按可空加；
                # 存量行随后由下面的 UPDATE 回填成模型默认值，不能留 NULL——
                # 布尔字段为 NULL 时，之后所有 .is_(False) 过滤都会静默漏掉这批老数据。
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'))
                applied.append(f"{table.name}.{col.name}")
                logger.warning("老库补列 %s.%s（代码比库新，属正常升级）", table.name, col.name)

                default_value = _scalar_default(col)
                if default_value is not _NO_DEFAULT:
                    # 走 Core UPDATE：列类型的 bind processor 会把 date/datetime
                    # 序列化成 SQLite 认识的字符串，直接 text()+参数会绑不上。
                    # 只在「本次补的新列」上执行——已存在的列（is_deleted 等）一律不动。
                    conn.execute(
                        table.update()
                        .where(table.c[col.name].is_(None))
                        .values({col.name: default_value})
                    )
                    applied.append(f"{table.name}.{col.name}:backfill")
                    logger.warning("老库补列回填 %s.%s（存量行按模型默认值填，不留 NULL）", table.name, col.name)

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
