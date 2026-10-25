"""D-S09：真实 PostgreSQL 状态流转，SMTP 是唯一外部服务替身。"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from backend.d import notification_worker as worker, notifications
import backend.d.seed as seed_module
from backend.d.mail_transport import MailConfig, MailConfigurationError, MailResult
from backend.d.models import Notification, NotificationDelivery, PublicConfig, User
from backend.shared.clock import format_time, utc_now

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONFIG = MailConfig('127.0.0.1', 1025, 'campusflow@example.test', 'none')


@pytest.fixture
def worker_db(isolated_db, monkeypatch, tmp_path, notification_worker_enabled):
    seed_module.seed()
    def factory(module):
        assert module == 'd'
        return isolated_db
    for service in (notifications, worker):
        monkeypatch.setattr(service, 'session_factory', factory)
    clock = tmp_path / 'clock.txt'
    clock.write_text(format_time(START), encoding='utf-8')
    monkeypatch.setenv('APP_ENV', 'test')
    monkeypatch.setenv('TEST_CLOCK_FILE', str(clock))
    monkeypatch.setattr(worker, 'load_mail_config', lambda: CONFIG)
    def unexpected_send(**kwargs):
        pytest.fail('This test must explicitly configure its SMTP boundary')
    monkeypatch.setattr(worker, 'send_email', unexpected_send)
    return isolated_db


def enqueue(*, recipients=None, request='worker-notice'):
    return notifications.send_notification(input={
        'requestId': request, 'sourceModule': 'M3', 'businessId': 'work-1', 'spaceId': 'sp-cs',
        'recipientUserIds': recipients or ['u-student1'], 'kind': 'M3_ASSIGNED',
        'title': '投递测试', 'body': '持久化正文',
    }, source_module='m3', actor_mode='SYSTEM', user_id=None)


def delivery(factory, user='u-student1'):
    with factory() as db:
        return db.scalar(select(NotificationDelivery).where(NotificationDelivery.recipient_user_id == user))


def advance(monkeypatch, value):
    monkeypatch.setattr(worker, 'utc_now', lambda: value)


def test_claim_is_durable_before_smtp_and_query_reports_completion(worker_db, monkeypatch):
    accepted = enqueue()
    calls = []
    def send(**kwargs):
        row = delivery(worker_db)
        assert row.status == 'SENDING' and row.attempt_count == 1
        assert row.attempt_token and row.lease_expires_at > row.last_attempt_at
        assert row.next_attempt_at is None
        calls.append(kwargs)
        return MailResult('SUBMITTED', 'SMTP_ACCEPTED')
    monkeypatch.setattr(worker, 'send_email', send)
    assert worker.run_batch() == 1
    row = delivery(worker_db)
    assert (row.status, row.attempt_count, row.reason) == ('SUBMITTED', 1, 'SMTP_ACCEPTED')
    assert row.attempt_token is None and row.lease_expires_at is None and row.next_attempt_at is None
    assert calls[0]['title'] == accepted['title'] and calls[0]['body'] == accepted['body']
    current = notifications.get_notification(input={'notificationId': accepted['notificationId']},
        source_module='m3', actor_mode='SYSTEM', user_id=None)
    assert current['deliveries'][0]['status'] == 'SUBMITTED'
    assert enqueue() == current
    assert worker.run_batch() == 0 and len(calls) == 1


@pytest.mark.parametrize('retry_limit', [0, 2, 5])
def test_only_due_retries_and_acceptance_snapshot_limit(worker_db, monkeypatch, retry_limit):
    with worker_db() as db, db.begin():
        db.get(PublicConfig, 1).notification_retry_limit = retry_limit
    enqueue()
    with worker_db() as db, db.begin():
        db.get(PublicConfig, 1).notification_retry_limit = 5 if retry_limit == 0 else 0
    calls = []
    def send(**kwargs):
        calls.append(kwargs['delivery_id'])
        return MailResult('TEMPORARY_FAILURE', 'SMTP_RCPT_450')
    monkeypatch.setattr(worker, 'send_email', send)
    for attempt in range(1, retry_limit + 2):
        assert worker.run_batch() == 1
        row = delivery(worker_db)
        assert row.attempt_count == attempt
        if attempt <= retry_limit:
            assert row.status == 'PENDING' and row.next_attempt_at > row.last_attempt_at
            assert worker.run_batch() == 0
            advance(monkeypatch, row.next_attempt_at - timedelta(microseconds=1))
            assert worker.run_batch() == 0
            advance(monkeypatch, row.next_attempt_at)
        else:
            assert row.status == 'FAILED' and row.next_attempt_at is None
    advance(monkeypatch, START + timedelta(days=1))
    assert worker.run_batch() == 0
    assert len(calls) == retry_limit + 1 and len(set(calls)) == 1


@pytest.mark.parametrize('outcome,status', [('PERMANENT_FAILURE', 'FAILED'), ('UNKNOWN', 'UNKNOWN')])
def test_terminal_failures_never_retry(worker_db, monkeypatch, outcome, status):
    enqueue()
    calls = []
    def send(**kwargs):
        calls.append(1)
        return MailResult(outcome, 'SMTP_TEST_RESULT')
    monkeypatch.setattr(worker, 'send_email', send)
    assert worker.run_batch() == 1
    advance(monkeypatch, START + timedelta(days=1))
    assert worker.run_batch() == 0 and calls == [1]
    assert delivery(worker_db).status == status
    assert enqueue()['deliveries'][0]['status'] == status


def test_each_recipient_has_independent_outcome(worker_db, monkeypatch):
    enqueue(recipients=['u-student1', 'u-student2'])
    responses = iter([MailResult('SUBMITTED', 'SMTP_ACCEPTED'), MailResult('UNKNOWN', 'SMTP_RESULT_UNKNOWN')])
    seen = []
    def send(**kwargs):
        seen.append(kwargs)
        return next(responses)
    monkeypatch.setattr(worker, 'send_email', send)
    assert worker.run_batch() == 2
    assert delivery(worker_db).status == 'SUBMITTED'
    assert delivery(worker_db, 'u-student2').status == 'UNKNOWN'
    assert len({r['recipient'] for r in seen}) == 2
    assert len({r['delivery_id'] for r in seen}) == 2


def test_temporary_failure_can_recover_once_at_due_time(worker_db, monkeypatch):
    enqueue()
    outcomes = iter([MailResult('TEMPORARY_FAILURE', 'SMTP_DATA_451'), MailResult('SUBMITTED', 'SMTP_ACCEPTED')])
    monkeypatch.setattr(worker, 'send_email', lambda **kw: next(outcomes))
    assert worker.run_batch() == 1
    advance(monkeypatch, delivery(worker_db).next_attempt_at)
    assert worker.run_batch() == 1
    row = delivery(worker_db)
    assert row.status == 'SUBMITTED' and row.attempt_count == 2 and row.next_attempt_at is None
    assert worker.run_batch() == 0


def test_disabled_user_does_not_start_an_attempt(worker_db):
    enqueue()
    with worker_db() as db, db.begin():
        db.get(User, 'u-student1').account_status = 'DISABLED'
    assert worker.run_batch() == 0
    row = delivery(worker_db)
    assert (row.status, row.reason, row.attempt_count) == ('FAILED', 'ACCOUNT_DISABLED', 0)
    assert row.last_attempt_at is None


def test_address_snapshot_is_not_replaced(worker_db, monkeypatch):
    enqueue()
    original = delivery(worker_db).recipient_email
    with worker_db() as db, db.begin():
        db.get(User, 'u-student1').email = 'changed@example.test'
    def send(**kwargs):
        assert kwargs['recipient'] == original
        return MailResult('SUBMITTED', 'SMTP_ACCEPTED')
    monkeypatch.setattr(worker, 'send_email', send)
    worker.run_batch()
    assert delivery(worker_db).recipient_email == original


def test_stale_token_cannot_complete_claim(worker_db):
    enqueue()
    claim = worker.claim_one()
    assert not worker.finish(replace(claim, token='wrong'), MailResult('SUBMITTED', 'SMTP_ACCEPTED'))
    assert delivery(worker_db).status == 'SENDING'
    assert worker.finish(claim, MailResult('SUBMITTED', 'SMTP_ACCEPTED'))
    assert not worker.finish(claim, MailResult('UNKNOWN', 'late'))
    assert delivery(worker_db).status == 'SUBMITTED'


@pytest.mark.parametrize('recover_first', [True, False])
def test_crash_or_expired_claim_becomes_unknown_never_reclaimed(worker_db, monkeypatch, recover_first):
    enqueue()
    claim = worker.claim_one()
    advance(monkeypatch, START + timedelta(seconds=worker.LEASE_SECONDS - 1))
    assert worker.expire_leases() == 0 and worker.claim_one() is None
    advance(monkeypatch, START + timedelta(seconds=worker.LEASE_SECONDS))
    if recover_first:
        assert worker.expire_leases() == 1
        assert not worker.finish(claim, MailResult('SUBMITTED', 'SMTP_ACCEPTED'))
    else:
        assert worker.finish(claim, MailResult('SUBMITTED', 'SMTP_ACCEPTED'))
    assert worker.expire_leases() == 0 and worker.run_batch() == 0
    row = delivery(worker_db)
    assert (row.status, row.reason, row.attempt_count) == ('UNKNOWN', 'WORKER_LEASE_EXPIRED', 1)


def test_invalid_config_preserves_pending_and_logs_no_exception_content(worker_db, monkeypatch, caplog):
    enqueue()
    def bad_config():
        raise MailConfigurationError('secret@example.test PASSWORD')
    monkeypatch.setattr(worker, 'load_mail_config', bad_config)
    with pytest.raises(RuntimeError):
        asyncio.run(worker.tick())
    assert delivery(worker_db).status == 'PENDING' and delivery(worker_db).attempt_count == 0
    assert 'MailConfigurationError' in caplog.text and 'PASSWORD' not in caplog.text


def test_unexpected_transport_failure_is_unknown_and_visible(worker_db, monkeypatch, caplog):
    enqueue()
    def fail(**kwargs):
        raise ValueError('secret@example.test PASSWORD')
    monkeypatch.setattr(worker, 'send_email', fail)
    with pytest.raises(RuntimeError):
        worker.run_batch()
    assert delivery(worker_db).status == 'UNKNOWN'
    assert 'worker-notice' in caplog.text and 'TRANSPORT_EXCEPTION_ValueError' in caplog.text
    assert 'PASSWORD' not in caplog.text and 'secret@example.test' not in caplog.text


def test_claim_commit_failure_never_calls_smtp(worker_db, monkeypatch):
    enqueue()
    class FailCommit(Session):
        pass
    def fail(db):
        raise RuntimeError('simulated commit failure')
    event.listen(FailCommit, 'before_commit', fail)
    failing = sessionmaker(bind=worker_db.kw['bind'], class_=FailCommit,
                          join_transaction_mode='create_savepoint', expire_on_commit=False)
    monkeypatch.setattr(worker, 'session_factory', lambda module: failing)
    monkeypatch.setattr(worker, 'expire_leases', lambda: 0)
    with pytest.raises(RuntimeError, match='simulated commit failure'):
        worker.run_batch()
    assert delivery(worker_db).status == 'PENDING'
    assert delivery(worker_db).attempt_count == 0


def test_result_persistence_failure_leaves_sending_then_unknown(worker_db, monkeypatch):
    enqueue()
    calls = []
    original_factory = worker.session_factory
    class FailCommit(Session):
        pass
    def reject_commit(db):
        raise RuntimeError('simulated result commit failure')
    event.listen(FailCommit, 'before_commit', reject_commit)
    failing = sessionmaker(bind=worker_db.kw['bind'], class_=FailCommit,
                          join_transaction_mode='create_savepoint', expire_on_commit=False)
    def send(**kwargs):
        calls.append(1)
        # 领取已经提交；让真实 finish 事务在提交前失败，验证回滚保留 SENDING。
        monkeypatch.setattr(worker, 'session_factory', lambda module: failing)
        return MailResult('SUBMITTED', 'SMTP_ACCEPTED')
    monkeypatch.setattr(worker, 'send_email', send)
    with pytest.raises(RuntimeError, match='persistence failed'):
        worker.run_batch()
    assert delivery(worker_db).status == 'SENDING'
    monkeypatch.setattr(worker, 'session_factory', original_factory)
    advance(monkeypatch, START + timedelta(seconds=worker.LEASE_SECONDS))
    assert worker.run_batch() == 0 and calls == [1]
    assert delivery(worker_db).status == 'UNKNOWN'


def test_bounded_batch_leaves_remaining_jobs_pending(worker_db, monkeypatch):
    for number in range(3):
        enqueue(request=f'batch-{number}')
    monkeypatch.setattr(worker, 'BATCH_SIZE', 2)
    monkeypatch.setattr(worker, 'send_email', lambda **kw: MailResult('SUBMITTED', 'SMTP_ACCEPTED'))
    assert worker.run_batch() == 2
    with worker_db() as db:
        statuses = db.scalars(select(NotificationDelivery.status)).all()
    assert sorted(statuses) == ['PENDING', 'SUBMITTED', 'SUBMITTED']
    assert worker.run_batch() == 1


def test_lifespan_registers_worker_reports_failure_and_recovers(monkeypatch):
    from backend.d.main import app
    monkeypatch.setattr(app.state, 'worker_error', False)
    calls, thread_ids = [], []
    def batch():
        thread_ids.append(threading.get_ident())
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('test failure')
        return 0
    monkeypatch.setattr(worker, 'run_batch', batch)
    async def check():
        main_thread = threading.get_ident()
        async with app.router.lifespan_context(app):
            for _ in range(100):
                if app.state.worker_error:
                    break
                await asyncio.sleep(.02)
            assert app.state.worker_error is True
            for _ in range(150):
                if len(calls) >= 2 and app.state.worker_error is False:
                    break
                await asyncio.sleep(.02)
            assert len(calls) >= 2 and app.state.worker_error is False
            assert all(value != main_thread for value in thread_ids)
        count = len(calls)
        await asyncio.sleep(.05)
        assert len(calls) == count
    asyncio.run(check())
