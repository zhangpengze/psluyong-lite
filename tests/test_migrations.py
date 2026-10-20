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
