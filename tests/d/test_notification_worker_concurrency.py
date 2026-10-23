"""D-S09：独立 PostgreSQL 连接验证领取互斥及过期结果竞争。"""
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

from backend.d import notifications, notification_worker as worker
import backend.d.seed as seed_module
from backend.d.models import Notification, NotificationDelivery
from backend.shared.clock import format_time
from backend.shared.db import session_factory as shared_session_factory
from backend.shared.errors import ApiError
from backend.shared.settings import ROOT, Settings

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def worker_concurrent_db(monkeypatch, tmp_path, notification_worker_enabled):
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

    schema = f"d_worker_test_{uuid4().hex}"
    assert re.fullmatch(r"d_worker_test_[0-9a-f]{32}", schema)
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

        for service in (seed_module, notifications, worker):
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
            assert re.fullmatch(r"d_worker_test_[0-9a-f]{32}", schema)
            with source_engine.begin() as cleanup:
                cleanup.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


from datetime import timedelta
from threading import Event, Lock
from backend.d.mail_transport import MailConfig, MailResult


def enqueue(recipients=None):
    return notifications.send_notification(input={
        'requestId': 'worker-race', 'sourceModule': 'M3', 'businessId': 'race', 'spaceId': 'sp-cs',
        'recipientUserIds': recipients or ['u-student1'], 'kind': 'M3_ASSIGNED',
        'title': 'Race', 'body': 'Concurrent SMTP boundary',
    }, source_module='m3', actor_mode='SYSTEM', user_id=None)


@pytest.fixture
def ready(worker_concurrent_db, monkeypatch):
    monkeypatch.setattr(worker, 'load_mail_config', lambda: MailConfig(
        '127.0.0.1', 1025, 'campusflow@example.test', 'none'))
    monkeypatch.setattr(worker, 'BATCH_SIZE', 1)
    def fail(**kwargs):
        pytest.fail('Explicit SMTP stub required')
    monkeypatch.setattr(worker, 'send_email', fail)
    return worker_concurrent_db


def test_other_worker_cannot_send_claimed_recipient(ready, monkeypatch):
    enqueue()
    entered, release = Event(), Event()
    calls = []
    def send(**kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return MailResult('SUBMITTED', 'SMTP_ACCEPTED')
    monkeypatch.setattr(worker, 'send_email', send)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(worker.run_batch)
        try:
            assert entered.wait(10)
            assert executor.submit(worker.run_batch).result(timeout=10) == 0
            # 另一条连接可见 SENDING，且 SMTP 等待期间没有持有数据库行锁。
            with ready() as db, db.begin():
                row = db.scalar(select(NotificationDelivery).with_for_update(nowait=True))
                assert row.status == 'SENDING' and row.attempt_count == 1
        finally:
            release.set()
        assert first.result(timeout=10) == 1
    assert calls == [1]


def test_locked_pending_is_skipped_without_waiting(ready):
    enqueue()
    with ready() as db, db.begin():
        db.scalar(select(NotificationDelivery).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(worker.claim_one).result(timeout=3) is None
    assert worker.claim_one() is not None


def test_two_workers_process_two_recipients_once(ready, monkeypatch):
    enqueue(['u-student1', 'u-student2'])
    barrier, lock, calls = Barrier(2), Lock(), []
    def send(**kwargs):
        with lock:
            calls.append(kwargs['delivery_id'])
        barrier.wait(timeout=10)
        return MailResult('SUBMITTED', 'SMTP_ACCEPTED')
    monkeypatch.setattr(worker, 'send_email', send)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(worker.run_batch), executor.submit(worker.run_batch)
        assert first.result(timeout=15) == second.result(timeout=15) == 1
    assert len(calls) == len(set(calls)) == 2
    with ready() as db:
        rows = db.scalars(select(NotificationDelivery)).all()
        assert all(r.status == 'SUBMITTED' and r.attempt_count == 1 for r in rows)


def test_expiration_and_late_completion_cannot_restore_or_resend(ready, monkeypatch):
    enqueue()
    claim = worker.claim_one()
    monkeypatch.setattr(worker, 'utc_now', lambda: _START + timedelta(seconds=worker.LEASE_SECONDS))
    barrier = Barrier(2)
    def expire():
        barrier.wait(timeout=10)
        return worker.expire_leases()
    def complete():
        barrier.wait(timeout=10)
        return worker.finish(claim, MailResult('SUBMITTED', 'SMTP_ACCEPTED'))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(expire), executor.submit(complete)
        first.result(timeout=15)
        second.result(timeout=15)
    with ready() as db:
        row = db.scalar(select(NotificationDelivery))
        assert row.status == 'UNKNOWN' and row.attempt_count == 1
        assert row.attempt_token is None and row.lease_expires_at is None
    assert worker.run_batch() == 0
