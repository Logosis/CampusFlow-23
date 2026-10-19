"""Exercise the actual revision stream in an empty isolated PostgreSQL schema."""
from uuid import uuid4
from pathlib import Path
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from backend.shared.settings import Settings
from backend.m3.models import Base


def test_empty_schema_upgrade_matches_models_downgrade_and_reupgrade(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    settings = Settings.load('m3')
    engine = create_engine(settings.database_url)
    schema = 'm3_migrate_' + uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))
    url = make_url(settings.database_url).update_query_dict({'options': f'-csearch_path={schema}'})
    monkeypatch.setenv('DATABASE_URL', url.render_as_string(hide_password=False))
    isolated = create_engine(url)
    cfg = Config(str(root / 'alembic.ini'))
    cfg.set_main_option('script_location', str(root / 'backend/m3/migrations'))
    try:
        command.upgrade(cfg, 'head')
        with isolated.connect() as connection:
            assert connection.scalar(text('select version_num from alembic_version')) == 'm3_0004'
            diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            assert diffs == []
        command.upgrade(cfg, 'head')
        command.downgrade(cfg, 'base')
        assert set(inspect(isolated).get_table_names()) == {'alembic_version'}
        command.upgrade(cfg, 'head')
        assert set(Base.metadata.tables).issubset(inspect(isolated).get_table_names())
    finally:
        isolated.dispose()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        engine.dispose()


def test_stage_exit_upgrade_backfills_equal_timestamp_history(monkeypatch):
    from datetime import datetime, timezone
    import sqlalchemy as sa
    root = Path(__file__).resolve().parents[2]
    settings = Settings.load('m3')
    schema = 'm3_upgrade_' + uuid4().hex
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))
    url = make_url(settings.database_url).update_query_dict({'options': f'-csearch_path={schema}'})
    monkeypatch.setenv('DATABASE_URL', url.render_as_string(hide_password=False))
    isolated = create_engine(url)
    cfg = Config(str(root / 'alembic.ini'))
    cfg.set_main_option('script_location', str(root / 'backend/m3/migrations'))
    try:
        command.upgrade(cfg, 'm3_0002')
        metadata = sa.MetaData()
        metadata.reflect(isolated)
        now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        with isolated.begin() as connection:
            connection.execute(metadata.tables['m3_worker_state'].insert().values(
                name='compensation', last_run=now))
            connection.execute(metadata.tables['m3_work_orders'].insert().values(id='work', reporter_id='u-student1',
                space_id='sp-school', department_id=None, fault_location='门口', description='旧数据',
                status='WAITING_ACCEPTANCE', version=4, assignee_id='u-technician', created_at=now,
                updated_at=now, stage_entered_at=now, first_repair_started_at=now,
                accepted_at=None, timeout_config_version=1))
            for i, (kind, stage) in enumerate([('REPORT', 'WAITING_ASSIGNMENT'), ('DISPATCH', 'ASSIGNED'),
                    ('REPAIR', 'REPAIRING'), ('COMPLETE', 'WAITING_ACCEPTANCE')], 1):
                connection.execute(metadata.tables['m3_records'].insert().values(id=f'record-{i}',
                    work_order_id='work', kind=kind, actor_id='u-admin', occurred_at=now, version=i,
                    summary=kind, data={}))
                connection.execute(metadata.tables['m3_stage_cycles'].insert().values(id=f'cycle-{i}',
                    work_order_id='work', stage=stage, cycle=1, config_version=1, rule={}, due_at=now,
                    entered_at=now, escalation_id=None))
        command.upgrade(cfg, 'head')
        with isolated.connect() as connection:
            rows = connection.execute(text('select id, exited_at from m3_stage_cycles order by id')).all()
            assert all(row.exited_at == now for row in rows[:3])
            assert rows[3].exited_at is None
            assert connection.scalar(text("select failed from m3_worker_state where name='compensation'")) is True
    finally:
        isolated.dispose()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        engine.dispose()
