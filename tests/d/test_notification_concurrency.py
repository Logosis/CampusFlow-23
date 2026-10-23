"""D-S09：独立 PostgreSQL 连接验证通知并发受理。"""
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

from backend.d import notifications
import backend.d.seed as seed_module
from backend.d.models import Notification, NotificationDelivery
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

        for service in (seed_module, notifications):
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



def send(body="original", peer="m3", actor="SYSTEM", user=None):
    try:
        return notifications.send_notification(
            input={"requestId": "notice-race", "sourceModule": peer.upper(), "businessId": "business-race",
                   "spaceId": "sp-cs", "recipientUserIds": ["u-student1", "u-student2"],
                   "kind": "M3_ASSIGNED" if peer == "m3" else "M2_APPROVED", "title": "Race", "body": body},
            actor_mode=actor, source_module=peer, user_id=user,
        )
    except ApiError as exc:
        return exc.code


def counts(factory):
    with factory() as db:
        return (db.scalar(select(func.count()).select_from(Notification)),
                db.scalar(select(func.count()).select_from(NotificationDelivery)))


def test_same_concurrent_request_creates_one_notice(concurrent_db):
    first, second = compete(send, send)
    assert isinstance(first, dict) and first == second
    assert counts(concurrent_db) == (1, 2)


def test_conflicting_concurrent_input_has_one_winner(concurrent_db):
    results = compete(send, lambda: send(body="changed"))
    success = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, str)]
    assert len(success) == 1 and failures == ["REQUEST_ID_REUSED"]
    assert counts(concurrent_db) == (1, 2)
    with concurrent_db() as db:
        assert db.scalar(select(Notification.body)) == success[0]["body"]


def test_different_sources_can_use_same_request_number(concurrent_db):
    first, second = compete(send, lambda: send(peer="m2"))
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["notificationId"] != second["notificationId"]
    assert counts(concurrent_db) == (2, 4)


def test_user_and_system_share_request_namespace(concurrent_db):
    first, second = compete(send, lambda: send(actor="USER", user="u-admin"))
    assert isinstance(first, dict) and first == second
    assert counts(concurrent_db) == (1, 2)
