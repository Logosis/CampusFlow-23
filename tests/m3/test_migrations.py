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
            assert connection.scalar(text('select version_num from alembic_version')) == 'm3_0002'
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
