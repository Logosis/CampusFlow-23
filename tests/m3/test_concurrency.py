"""Force overlapping database operations; only the D/M1 boundary is simulated."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Event
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text

from backend.m3 import domain, peers, worker
from backend.m3.models import Outbox, Receipt, Record, StageCycle, WorkOrder
from backend.m3.seed import seed_async
from tests.m3.test_lifecycle import assigned, mutate
from tests.m3.test_report_dispatch import report


def post(app, function, body, token):
    # Separate portals let synchronous SQL execute concurrently in distinct requests.
    with TestClient(app) as client:
        return client.post('/api/v1/m3/' + function, json=body,
                           headers={'Authorization': 'Bearer ' + token})


def locked_race(factory, operations, *, work_id=None, receipt_key=None):
    """Hold a real lock until both contenders are observed waiting in PostgreSQL."""
    engine = factory.kw['bind']
    arrivals = Queue()

    def observe(connection, cursor, statement, parameters, context, executemany):
        matches = ('m3_work_orders' in statement and 'FOR UPDATE' in statement
                   if work_id else 'pg_advisory_xact_lock' in statement)
        if matches:
            arrivals.put(connection.connection.driver_connection.info.backend_pid)

    def wait_for_lock(pid):
        deadline = monotonic() + 5
        while monotonic() < deadline:
            with engine.connect() as connection:
                waiting = connection.scalar(text(
                    'SELECT count(*) FROM pg_locks WHERE pid=:pid AND NOT granted'), {'pid': pid})
            if waiting:
                return
            sleep(.01)
        pytest.fail('Contender did not reach a real PostgreSQL lock wait')

    blocker = factory()
    try:
        if work_id:
            domain.get_order(blocker, work_id, locked=True)
        else:
            domain.lock_key(blocker, receipt_key)
        event.listen(engine, 'before_cursor_execute', observe)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = []
            try:
                for operation in operations:
                    futures.append(pool.submit(operation))
                    wait_for_lock(arrivals.get(timeout=5))
            finally:
                blocker.rollback()
                event.remove(engine, 'before_cursor_execute', observe)
            return [future.result(timeout=10) for future in futures]
    finally:
        blocker.close()


@pytest.mark.parametrize('changed_content', [False, True])
def test_same_report_request_concurrent_creates_once(api, db_factory, peers_double, changed_content):
    body = dict(spaceId='sp-school', faultLocation='门口', description='灯不亮', requestId='same-report')
    second = body | {'description': '另一个内容'} if changed_content else body
    responses = locked_race(db_factory, [
        lambda: post(api.app, 'report_fault', body, 'student1'),
        lambda: post(api.app, 'report_fault', second, 'student1'),
    ], receipt_key=domain.canonical(['u-student1', 'report_fault', body['requestId']]))
    assert sorted(r.status_code for r in responses) == ([200, 409] if changed_content else [200, 200])
    if changed_content:
        assert next(r for r in responses if r.status_code == 409).json()['error']['code'] == 'REQUEST_ID_REUSED'
    else:
        assert responses[0].json()['data'] == responses[1].json()['data']
    with db_factory() as session:
        assert session.scalar(select(func.count()).select_from(WorkOrder)) == 1
        assert session.scalar(select(func.count()).select_from(Record)) == 1
        assert session.scalar(select(func.count()).select_from(Receipt)) == 1
        job = session.scalar(select(Outbox))
        assert job.kind == 'AUDIT' and job.status == 'SUCCESS'
    writes = [c for c in peers_double.calls if c[1] == 'audit' and c[2]['operation'] == 'APPEND']
    assert len(writes) == 1


@pytest.mark.parametrize('same_request', [False, True])
def test_concurrent_completion_is_recorded_once(api, call, db_factory, peers_double, same_request):
    w = mutate(call, 'start_repair', assigned(call), 'technician')['workOrder']
    body = dict(workOrderId=w['workOrderId'], expectedVersion=w['version'], summary='修复完成', requestId='complete-1')
    second = body if same_request else body | {'requestId': 'complete-2'}
    options = (dict(receipt_key=domain.canonical(['u-technician', 'submit_completion', body['requestId']]))
               if same_request else dict(work_id=w['workOrderId']))
    responses = locked_race(db_factory, [
        lambda: post(api.app, 'submit_completion', body, 'technician'),
        lambda: post(api.app, 'submit_completion', second, 'technician'),
    ], **options)
    assert sorted(r.status_code for r in responses) == ([200, 200] if same_request else [200, 409])
    if same_request:
        assert responses[0].json()['data']['recordId'] == responses[1].json()['data']['recordId']
    with db_factory() as session:
        current = session.get(WorkOrder, w['workOrderId'])
        assert current.status == 'WAITING_ACCEPTANCE' and current.version == w['version'] + 1
        assert session.scalar(select(func.count()).select_from(Record).where(Record.kind == 'COMPLETE')) == 1
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == 'SEAT')) == 1
    sends = [c for c in peers_double.calls if c[1] == 'send_notification' and c[2]['kind'] == 'M3_ACCEPTANCE_PENDING']
    assert len(sends) == 1


def test_concurrent_evaluations_are_recorded_once(api, call, db_factory, peers_double):
    w = mutate(call, 'start_repair', assigned(call), 'technician')['workOrder']
    complete = mutate(call, 'submit_completion', w, 'technician', summary='完成')
    accepted = mutate(call, 'submit_acceptance', complete['workOrder'], 'inspector',
                      completionId=complete['recordId'], decision='PASS')['workOrder']
    body = dict(workOrderId=w['workOrderId'], content='已恢复正常', requestId='evaluation-1')
    before = len(peers_double.calls)
    responses = locked_race(db_factory, [
        lambda: post(api.app, 'submit_evaluation', body, 'student1'),
        lambda: post(api.app, 'submit_evaluation', body | {'requestId': 'evaluation-2'}, 'student1'),
    ], work_id=w['workOrderId'])
    assert sorted(r.status_code for r in responses) == [200, 409]
    with db_factory() as session:
        current = session.get(WorkOrder, w['workOrderId'])
        assert current.status == 'ACCEPTED' and current.version == accepted['version'] + 1
        assert session.scalar(select(func.count()).select_from(Record).where(Record.kind == 'EVALUATE')) == 1
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == 'SEAT')) == 2
    assert not any(c[0] == 'm1' and c[1] == 'change_room_limit' for c in peers_double.calls[before:])


@pytest.mark.parametrize('ending', ['CANCEL', 'ACCEPT'])
@pytest.mark.parametrize('scan_first', [False, True])
def test_timeout_scan_competes_with_terminal_transition(api, call, db_factory, peers_double, now, ending, scan_first):
    asyncio.run(seed_async())
    if ending == 'CANCEL':
        peers_double.grant('student1', 'm3.work_order.cancel')
        w = report(call)['workOrder']
        function, token, extra = 'cancel_work_order', 'student1', {}
    else:
        w = mutate(call, 'start_repair', assigned(call), 'technician')['workOrder']
        complete = mutate(call, 'submit_completion', w, 'technician', summary='完成')
        w = complete['workOrder']
        function, token = 'submit_acceptance', 'inspector'
        extra = dict(completionId=complete['recordId'], decision='PASS')
    now[0] += timedelta(hours=24, seconds=1)
    body = dict(workOrderId=w['workOrderId'], expectedVersion=w['version'], requestId='end') | extra
    transition = lambda: post(api.app, function, body, token)
    operations = [worker._scan_due, transition] if scan_first else [transition, worker._scan_due]
    responses = locked_race(db_factory, operations, work_id=w['workOrderId'])
    assert responses[1 if scan_first else 0].status_code == 200
    # A scan that won the lock may escalate the active stage; a terminated stage cannot.
    with db_factory() as session:
        current = session.get(WorkOrder, w['workOrderId'])
        assert current.status == ('CANCELLED' if ending == 'CANCEL' else 'ACCEPTED')
        assert current.version == w['version'] + 1
        escalations = list(session.scalars(select(Record).where(Record.kind == 'ESCALATE')))
        assert len(escalations) == int(scan_first)
        assert all(r.version == w['version'] and r.data['stage'] == w['status'] for r in escalations)
        cycle = session.scalar(select(StageCycle).where(StageCycle.work_order_id == w['workOrderId'],
                                                       StageCycle.stage == w['status']))
        assert cycle.exited_at is not None
        before = len(escalations)
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == 'SEAT')) == (0 if ending == 'CANCEL' else 2)
    now[0] += timedelta(seconds=60)
    worker._scan_due()
    with db_factory() as session:
        assert session.scalar(select(func.count()).select_from(Record).where(Record.kind == 'ESCALATE')) == before
    run_result = asyncio.run(worker.process_jobs())
    assert run_result is False
    sends = [c for c in peers_double.calls if c[1] == 'send_notification' and c[2]['kind'] == 'M3_TIMEOUT_ESCALATED']
    assert len(sends) == before


def test_workers_contend_for_audit_without_duplicate_write(call, db_factory, peers_double, monkeypatch):
    peers_double.fail['d', 'audit'] = 'UNAVAILABLE'
    report(call)
    del peers_double.fail['d', 'audit']
    with db_factory() as session:
        job = session.scalar(select(Outbox))
        request_id, original_payload = job.request_id, job.payload
    entered, release = Event(), Event()
    original_peer = peers.peer

    async def blocked_peer(target, function, body, request=None):
        if target == 'd' and function == 'audit' and body['requestId'] == request_id and body['operation'] == 'GET':
            entered.set()
            assert release.wait(timeout=5)
        return await original_peer(target, function, body, request)

    monkeypatch.setattr(peers, 'peer', blocked_peer)
    before = len(peers_double.calls)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(asyncio.run, worker.process_jobs())
        try:
            assert entered.wait(timeout=5)
            second = pool.submit(asyncio.run, worker.process_jobs())
            assert second.result(timeout=5) is True  # the first worker still owns the lease
        finally:
            release.set()
        assert first.result(timeout=5) is False
    with db_factory() as session:
        current = session.get(Outbox, job.id)
        assert current.status == 'SUCCESS' and current.lease_until is None
        assert current.request_id == request_id and current.payload == original_payload
    calls = [c for c in peers_double.calls[before:] if c[1] == 'audit']
    assert [c[2]['operation'] for c in calls] == ['GET', 'APPEND']
    assert all(c[2]['requestId'] == request_id for c in calls)
