"""D-S11：独立PostgreSQL连接验证审计并发幂等。"""
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

from backend.d import audit
import backend.d.seed as seed_module
from backend.d.models import AuditEvent
from backend.shared.clock import format_time
from backend.shared.db import session_factory as shared_session_factory
from backend.shared.errors import ApiError
from backend.shared.settings import ROOT, Settings

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def audit_concurrent_db(monkeypatch, tmp_path):
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

    schema = f"d_audit_test_{uuid4().hex}"
    assert re.fullmatch(r"d_audit_test_[0-9a-f]{32}", schema)
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

        for service in (seed_module, audit):
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
            assert re.fullmatch(r"d_audit_test_[0-9a-f]{32}", schema)
            with source_engine.begin() as cleanup:
                cleanup.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


def append(summary='unchanged', peer='m3', mode='SYSTEM'):
    try:
        return audit.audit(input={
            'operation': 'APPEND', 'requestId': 'audit-race', 'sourceModule': peer.upper(),
            'actorType': 'USER', 'actorId': 'u-admin', 'objectType': 'WORK_ORDER', 'businessId': 'work',
            'spaceId': 'sp-cs', 'action': 'DISPATCH', 'occurredAt': format_time(_START),
            'result': 'SUCCESS', 'summary': summary,
        }, actor_mode=mode, source_module=peer, user_id='u-admin' if mode == 'USER' else None)
    except ApiError as exc:
        return exc.code


def compete(first, second):
    barrier = Barrier(2)
    def run(operation):
        barrier.wait(timeout=10)
        return operation()
    with ThreadPoolExecutor(max_workers=2) as executor:
        left, right = executor.submit(run, first), executor.submit(run, second)
        return left.result(timeout=30), right.result(timeout=30)


def count(factory):
    with factory() as db:
        return db.scalar(select(func.count()).select_from(AuditEvent))


def test_duplicate_concurrent_append_has_one_event(audit_concurrent_db):
    left, right = compete(append, append)
    assert isinstance(left, dict) and left == right and count(audit_concurrent_db) == 1


def test_changed_input_has_one_winner(audit_concurrent_db):
    results = compete(append, lambda: append(summary='different'))
    assert len([r for r in results if isinstance(r, dict)]) == 1
    assert [r for r in results if isinstance(r, str)] == ['REQUEST_ID_REUSED']
    assert count(audit_concurrent_db) == 1


def test_other_source_uses_separate_namespace(audit_concurrent_db):
    left, right = compete(append, lambda: append(peer='m2'))
    assert isinstance(left, dict) and isinstance(right, dict) and left['auditId'] != right['auditId']
    assert count(audit_concurrent_db) == 2


def test_user_and_system_same_event_share_receipt_and_verifier(audit_concurrent_db):
    left, right = compete(append, lambda: append(mode='USER'))
    assert isinstance(left, dict) and left == right and left['verifiedBy'] in {'M3', 'D'}
    assert count(audit_concurrent_db) == 1
