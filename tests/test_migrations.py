"""启动迁移测试：老库缺列/缺索引时自动补齐"""

from __future__ import annotations

from datetime import date

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


def test_migration_backfills_default_for_new_column(app, clean_db):
    """老库补列后，存量行必须回填成模型默认值而不是 NULL。

    留 NULL 的代价：未来新增布尔字段时，老数据会从 `.is_(False)` 过滤里
    静默消失——列表里看不到、统计里也没有，还没有任何报错。
    """
    from server.app.models import Record

    with app.app_context():
        # 存量行先落库，再模拟"老库缺列"
        db.session.add(Record(work_date=date(2026, 10, 21), employee_name="张三", template_name="模板A"))
        db.session.commit()

        db.session.execute(text("ALTER TABLE records DROP COLUMN processing_ms"))
        db.session.commit()

        applied = run_migrations()

        assert "records.processing_ms" in applied
        assert "records.processing_ms:backfill" in applied
        values = [row[0] for row in db.session.execute(text("SELECT processing_ms FROM records"))]
        assert values, "应有存量行"
        assert all(v is not None for v in values), "补列后存量行不能留 NULL"
        assert all(v == 0 for v in values), "应回填模型默认值 0"


def test_migration_does_not_touch_existing_columns(app, clean_db):
    """已有列（is_deleted 等）的行为一律不动：结构最新时迁移零变更、零回填"""
    from server.app.models import Record

    with app.app_context():
        assert run_migrations() == []
        # 存量行的 is_deleted 保持原值，不会被"顺手回填"覆盖
        db.session.add(
            Record(work_date=date(2026, 10, 21), employee_name="张三", template_name="模板A", is_deleted=True)
        )
        db.session.commit()
        assert run_migrations() == []
        rows = db.session.execute(text("SELECT is_deleted FROM records")).fetchall()
        assert rows and rows[0][0] == 1


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
