"""D 数据库测试共用准备：实际迁移、独立 schema、结束后整体回滚。"""

import json
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import backend.d.seed as seed_module
from backend.shared.db import session_factory as shared_session_factory
from backend.shared.settings import ROOT, Settings


@pytest.fixture
def fixture_data():
    path = ROOT / "tests" / "fixtures" / "bootstrap.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def isolated_db(monkeypatch):
    """所有测试写入独立 schema；seed 的提交只提交内部保存点。"""
    if Settings.load("d").app_env != "test":
        pytest.fail("这些测试要求已有配置 APP_ENV=test。", pytrace=False)

    with shared_session_factory("d")() as session:
        engine = session.get_bind()
    if engine.dialect.name != "postgresql":
        pytest.fail("这些测试要求连接 PostgreSQL。", pytrace=False)

    connection_error_type = None
    try:
        connection = engine.connect()
    except SQLAlchemyError as exc:
        connection_error_type = type(exc).__name__
    if connection_error_type is not None:
        pytest.fail(
            f"D test database connection failed ({connection_error_type}); "
            "check PostgreSQL and local configuration.",
            pytrace=False,
        )

    with connection:
        transaction = connection.begin()
        try:
            schema = f"d_seed_test_{uuid4().hex}"
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == schema

            scripts = ScriptDirectory(str(ROOT / "backend" / "d" / "migrations"))
            revisions = list(scripts.walk_revisions())
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                for revision in reversed(revisions):
                    revision.module.upgrade()

            factory = sessionmaker(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )

            def test_session_factory(module):
                assert module == "d", "D seed 只能访问 D 数据库"
                return factory

            monkeypatch.setattr(seed_module, "session_factory", test_session_factory)
            yield factory
        finally:
            # PostgreSQL 会同时回滚 schema、建表以及测试数据。
            if transaction.is_active:
                transaction.rollback()



@pytest.fixture(autouse=True)
def pause_background_notifications(monkeypatch):
    """HTTP 用例仅验接口；禁止其 lifespan 访问真实队列或干扰隔离事务。

    worker 专项用例通过 notification_worker_enabled 显式恢复真实批处理。
    """
    from backend.d import notification_worker
    original = notification_worker.run_batch
    monkeypatch.setattr(notification_worker, 'run_batch', lambda: 0)
    return original


@pytest.fixture
def notification_worker_enabled(pause_background_notifications, monkeypatch):
    from backend.d import notification_worker
    monkeypatch.setattr(notification_worker, 'run_batch', pause_background_notifications)
