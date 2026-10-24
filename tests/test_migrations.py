"""启动迁移测试：老库缺列/缺索引时自动补齐"""

from __future__ import annotations

from sqlalchemy import inspect, text

from server.app.db import db
from server.app.migrations import run_migrations


def test_migration_readds_dropped_column(app):
    """模拟"新代码加了字段、老库还没升级"：删列后迁移应补回"""
    with app.app_context():
        db.session.execute(text("ALTER TABLE records DROP COLUMN warnings"))
        db.session.commit()
        assert "warnings" not in {c["name"] for c in inspect(db.engine).get_columns("records")}

        applied = run_migrations()

        cols = {c["name"] for c in inspect(db.engine).get_columns("records")}
        assert "warnings" in cols
        assert "records.warnings" in applied


def test_migration_readds_dropped_index(app):
    with app.app_context():
        db.session.execute(text("DROP INDEX idx_records_employee_date"))
        db.session.commit()
        assert "idx_records_employee_date" not in {
            i["name"] for i in inspect(db.engine).get_indexes("records")
        }

        run_migrations()

        assert "idx_records_employee_date" in {
            i["name"] for i in inspect(db.engine).get_indexes("records")
        }


def test_migration_is_idempotent(app):
    """结构已是最新时迁移必须零变更，不能每次启动都报 warning"""
    with app.app_context():
        assert run_migrations() == []
        assert run_migrations() == []


def test_migration_drops_deprecated_dedup_index(app):
    """老库的旧去重索引必须被删掉。

    去重键从 (work_date, author, title) 放宽到带 employee_name 之后，
    如果旧索引还留在库里，数据库仍然会按旧规则拦——表现为"代码明明改了，
    线上还是 409"，最难查的那类问题。
    """
    with app.app_context():
        db.session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_records_date_author_title_active "
                "ON records (work_date, author, title) WHERE is_deleted = 0"
            )
        )
        db.session.commit()
        assert "uq_records_date_author_title_active" in {
            i["name"] for i in inspect(db.engine).get_indexes("records")
        }

        applied = run_migrations()

        names = {i["name"] for i in inspect(db.engine).get_indexes("records")}
        assert "uq_records_date_author_title_active" not in names
        assert "uq_records_dedup_success" in names
        assert "drop-index:uq_records_date_author_title_active" in applied


def test_migration_replaces_old_active_dedup_index(app):
    """上一版留下的 uq_records_dedup_active 也要删：它不区分 status，
    会继续把"失败后重试"拦成 409。新索引只约束成功记录。"""
    with app.app_context():
        db.session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_records_dedup_active "
                "ON records (work_date, employee_name, template_name, author, title) "
                "WHERE is_deleted = 0"
            )
        )
        db.session.commit()

        run_migrations()

        names = {i["name"] for i in inspect(db.engine).get_indexes("records")}
        assert "uq_records_dedup_active" not in names
        assert "uq_records_dedup_success" in names

        # 新索引必须是"只约束成功记录"的部分索引
        ddl = db.session.execute(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_records_dedup_success'")
        ).scalar()
        assert ddl and "status" in ddl and "success" in ddl
