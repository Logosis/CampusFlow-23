"""D-S12：独立 PostgreSQL 连接验证重复与版本竞争。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re
from threading import Barrier
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.d import public_config
import backend.d.seed as seed_module
from backend.d.models import PublicConfig, PublicConfigRequest
from backend.shared.clock import format_time
from backend.shared.db import session_factory as shared_session_factory
from backend.shared.errors import ApiError
from backend.shared.settings import ROOT, Settings

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def concurrent_db(monkeypatch, tmp_path):
    """仅本用例随机 schema 提交数据，让不同连接可见；结束后清理该 schema。"""
    if Settings.load("d").app_env != "test":
        pytest.fail("Concurrency checks require APP_ENV=test.", pytrace=False)
    with shared_session_factory("d")() as db:
        source_engine = db.get_bind()
    if source_engine.dialect.name != "postgresql":
        pytest.fail("Concurrency checks require PostgreSQL.", pytrace=False)

    connection_error = None
    try:
        connection = source_engine.connect()
    except SQLAlchemyError as exc:
        connection_error = type(exc).__name__
    if connection_error is not None:
        pytest.fail(f"D test database connection failed ({connection_error}).", pytrace=False)

    schema = f"d_concurrency_test_{uuid4().hex}"
    assert re.fullmatch(r"d_concurrency_test_[0-9a-f]{32}", schema)
    test_engine = None
    created = False
    try:
        with connection, connection.begin():
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == schema
            scripts = ScriptDirectory(str(ROOT / "backend" / "d" / "migrations"))
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                for revision in reversed(list(scripts.walk_revisions())):
                    revision.module.upgrade()
        created = True

        # 每个工作线程取得自己的连接/事务，只能在随机测试 schema 中访问业务表。
        test_engine = create_engine(
            source_engine.url,
            connect_args={
                "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=10000",
                "connect_timeout": 5,
            },
            pool_size=3,
            max_overflow=0,
            pool_timeout=10,
        )
        with test_engine.connect() as check:
            assert check.exec_driver_sql("SELECT current_schema()").scalar_one() == schema
        factory = sessionmaker(bind=test_engine, expire_on_commit=False)

        def test_factory(module):
            assert module == "d"
            return factory

        for service in (seed_module, public_config):
            monkeypatch.setattr(service, "session_factory", test_factory)
        clock_file = tmp_path / "clock.txt"
        clock_file.write_text(format_time(_START), encoding="utf-8")
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("TEST_CLOCK_FILE", str(clock_file))
        seed_module.seed()
        yield factory
    finally:
        if test_engine is not None:
            test_engine.dispose()
        if created:
            # 名称仅来自本 fixture 的随机值，绝不清空 public 或其他 schema。
            assert re.fullmatch(r"d_concurrency_test_[0-9a-f]{32}", schema)
            with source_engine.begin() as cleanup:
                cleanup.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


def compete(first, second):
    barrier = Barrier(2)

    def run(operation):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=2) as executor:
        left = executor.submit(run, first)
        right = executor.submit(run, second)
        return left.result(timeout=30), right.result(timeout=30)



def update(request_id="config-race-1", limit=3):
    try:
        return public_config.manage_public_config(
            input={"operation": "UPDATE", "authMethod": "PASSWORD", "notificationChannel": "EMAIL",
                   "notificationRetryLimit": limit, "expectedVersion": 1, "requestId": request_id},
            actor_mode="USER", source_module="m1", user_id="u-admin",
        )
    except ApiError as exc:
        return exc.code


def test_concurrent_same_request_is_applied_once(concurrent_db):
    first, second = compete(update, update)
    assert first == second == {"authMethod": "PASSWORD", "notificationChannel": "EMAIL",
                               "notificationRetryLimit": 3, "version": 2}
    with concurrent_db() as db:
        assert db.get(PublicConfig, 1).version == 2
        assert db.scalar(select(func.count()).select_from(PublicConfigRequest)) == 1


def test_concurrent_different_requests_cannot_overwrite_same_version(concurrent_db):
    results = compete(update, lambda: update("config-race-2", 4))
    successes = [item for item in results if isinstance(item, dict)]
    assert len(successes) == 1 and "CONFLICT" in results
    with concurrent_db() as db:
        row = db.get(PublicConfig, 1)
        assert row.version == 2 and row.notification_retry_limit == successes[0]["notificationRetryLimit"]
        assert db.scalar(select(func.count()).select_from(PublicConfigRequest)) == 1


def test_concurrent_same_id_changed_content_is_rejected(concurrent_db):
    results = compete(update, lambda: update(limit=4))
    successes = [item for item in results if isinstance(item, dict)]
    assert len(successes) == 1 and "REQUEST_ID_REUSED" in results
    with concurrent_db() as db:
        assert db.get(PublicConfig, 1).version == 2
        record = db.scalars(select(PublicConfigRequest)).one()
        assert record.response_data == successes[0]
