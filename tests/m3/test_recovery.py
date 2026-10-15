import asyncio
from datetime import timedelta
from sqlalchemy import select, func
from backend.m3 import worker
from backend.m3.models import Outbox, Record, TimeoutConfig, StageCycle
from backend.m3.seed import seed_async
from tests.m3.conftest import FIXTURE
from tests.m3.test_lifecycle import assigned, mutate
from tests.m3.test_report_dispatch import report


def run_jobs(work_id=None):
    return asyncio.run(worker.process_jobs(work_id))


def test_unknown_repair_reconciles_original_id_and_release_overtakes_it(call, peers_double, db_factory):
    peers_double.fail['m1', 'change_room_limit'] = 'TRANSPORT'
    started = mutate(call, 'start_repair', assigned(call), 'technician')
    assert started['workOrder']['status'] == 'REPAIRING'
    assert started['seatSync']['status'] == 'UNKNOWN'
    repair_request = started['seatSync']['requestId']
    assert started['seatSync']['checkedAt'] is None
    completed = mutate(call, 'submit_completion', started['workOrder'], 'technician', summary='done')
    del peers_double.fail['m1', 'change_room_limit']
    accepted = mutate(call, 'submit_acceptance', completed['workOrder'], 'inspector',
        completionId=completed['recordId'], decision='PASS')
    assert accepted['seatSync']['status'] == 'SUCCESS'
    assert accepted['seatSync']['sourceVersion'] > started['seatSync']['sourceVersion']
    assert accepted['seatSync']['result']['currentLimit']['state'] == 'RELEASED'
    before = len([c for c in peers_double.calls if c[0:2] == ('m1', 'change_room_limit')])
    run_jobs()
    after = len([c for c in peers_double.calls if c[0:2] == ('m1', 'change_room_limit')])
    assert before == after  # obsolete REPAIR is queried, never resent after RELEASE
    assert any(c[1] == 'get_room_limit' and c[2]['requestId'] == repair_request for c in peers_double.calls)
    with db_factory() as session:
        old = session.scalar(select(Outbox).where(Outbox.request_id == repair_request))
        assert old.status == 'UNKNOWN'


def test_unknown_write_retries_same_request_after_unknown_lookup(call, peers_double):
    w = assigned(call)
    peers_double.fail['m1', 'change_room_limit'] = 'UNAVAILABLE'
    result = mutate(call, 'start_repair', w, 'technician')
    key = result['seatSync']['requestId']
    del peers_double.fail['m1', 'change_room_limit']
    assert not run_jobs()
    writes = [c[2] for c in peers_double.calls if c[0:2] == ('m1', 'change_room_limit')]
    assert len(writes) == 2 and writes[0] == writes[1]
    assert writes[1]['requestId'] == key
    detail = call('get_work_tasks', {'purpose': 'REPAIR', 'workOrderId': w['workOrderId']}, 'technician')['items'][0]
    assert detail['seatSync']['status'] == 'SUCCESS'


def test_known_rejection_not_retried_and_query_error_does_not_reject(call, peers_double):
    peers_double.fail['m1', 'change_room_limit'] = 'CONFLICT'
    started = mutate(call, 'start_repair', assigned(call), 'technician')
    assert started['seatSync']['status'] == 'REJECTED'
    before = len(peers_double.calls)
    run_jobs()
    assert not any(c[0] == 'm1' for c in peers_double.calls[before:])
    peers_double.fail['m1', 'change_room_limit'] = 'UNAVAILABLE'
    started = mutate(call, 'start_repair', assigned(call), 'technician')
    peers_double.fail['m1', 'get_room_limit'] = 'FORBIDDEN'
    run_jobs()
    detail = call('get_work_tasks', {'purpose': 'REPAIR', 'workOrderId': started['workOrder']['workOrderId']}, 'technician')['items'][0]
    assert detail['seatSync']['status'] == 'UNKNOWN'


def test_notification_unknown_only_queried_no_resend_and_audit_retries(call, peers_double, db_factory):
    peers_double.fail['d', 'send_notification'] = 'TRANSPORT'
    peers_double.fail['d', 'audit'] = 'UNAVAILABLE'
    w = assigned(call)
    with db_factory() as s:
        jobs = list(s.scalars(select(Outbox).where(Outbox.kind == 'NOTIFICATION')))
        key = jobs[0].request_id
        assert jobs[0].status == 'UNKNOWN'
    del peers_double.fail['d', 'send_notification']
    del peers_double.fail['d', 'audit']
    run_jobs()
    assert len([c for c in peers_double.calls if c[1] == 'send_notification' and c[2]['requestId'] == key]) == 1
    assert any(c[1] == 'get_notification' and c[2]['requestId'] == key for c in peers_double.calls)
    with db_factory() as s:
        assert all(j.status == 'SUCCESS' for j in s.scalars(select(Outbox).where(Outbox.kind == 'AUDIT')))
        assert s.scalar(select(Outbox).where(Outbox.request_id == key)).status == 'UNKNOWN'


def test_malformed_external_result_never_becomes_success(call, peers_double):
    peers_double.responses['m1', 'change_room_limit'] = {'requestStatus': 'SUCCESS'}
    started = mutate(call, 'start_repair', assigned(call), 'technician')
    assert started['seatSync']['status'] == 'UNKNOWN' and started['seatSync']['result'] is None


def test_timeout_each_cycle_once_and_no_automatic_release(call, now, db_factory, peers_double):
    asyncio.run(seed_async())
    w = report(call)['workOrder']
    now[0] += timedelta(hours=24)
    worker._scan_due()
    run_jobs()
    worker._scan_due()
    with db_factory() as s:
        events = list(s.scalars(select(Record).where(Record.kind == 'ESCALATE')))
        assert len(events) == 1 and events[0].data['configVersion'] == 1
        jobs = list(s.scalars(select(Outbox).where(Outbox.kind == 'NOTIFICATION')))
        assert jobs[0].payload['recipientUserIds'] == ['u-admin', 'u-supervisor']
        assert s.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == 'SEAT')) == 0
    dispatched = call('dispatch_order', dict(workOrderId=w['workOrderId'], assigneeId='u-technician', expectedVersion=1, requestId='d'), 'dispatcher')
    w = mutate(call, 'start_repair', dispatched['workOrder'], 'technician')['workOrder']
    now[0] += timedelta(hours=24)
    worker._scan_due()
    completed = mutate(call, 'submit_completion', w, 'technician', summary='done')
    returned = mutate(call, 'submit_acceptance', completed['workOrder'], 'inspector', completionId=completed['recordId'], decision='FAIL')
    now[0] += timedelta(hours=24)
    worker._scan_due()
    with db_factory() as s:
        repairing = list(s.scalars(select(Record).where(Record.kind == 'ESCALATE')))
        assert [(r.data['stage'], r.data['cycle']) for r in repairing] == [('WAITING_ASSIGNMENT', 1), ('REPAIRING', 1), ('REPAIRING', 2)]


def test_seed_twice_preserves_configuration_and_business(call, db_factory):
    asyncio.run(seed_async())
    w = report(call)['workOrder']
    with db_factory() as s, s.begin():
        row = s.get(TimeoutConfig, 1)
        row.data = row.data | {'version': 5}
    asyncio.run(seed_async())
    with db_factory() as s:
        assert s.get(TimeoutConfig, 1).data['version'] == 5
        cycle = s.scalar(select(StageCycle).where(StageCycle.work_order_id == w['workOrderId']))
        assert cycle.config_version == 1 and cycle.rule['timeoutSeconds'] == 86400


def test_lost_response_and_expired_lease_recover_from_persisted_original_receipt(call, peers_double, db_factory, now):
    peers_double.fail['m1', 'change_room_limit'] = 'TRANSPORT'
    result = mutate(call, 'start_repair', assigned(call), 'technician')
    key = result['seatSync']['requestId']
    with db_factory() as session, session.begin():
        job = session.scalar(select(Outbox).where(Outbox.request_id == key))
        payload = job.payload
        job.lease_until = now[0] - timedelta(seconds=1)
    # The remote side committed, but its response was lost and the sender restarted.
    peers_double.limits[payload['businessId']] = {k: payload[k] for k in ('source', 'businessId', 'spaceId', 'startTime', 'endTime', 'sourceVersion')} | {'state': 'ACTIVE'}
    peers_double.requests[key] = 'SUCCESS'
    before = len([c for c in peers_double.calls if c[0:2] == ('m1', 'change_room_limit')])
    del peers_double.fail['m1', 'change_room_limit']
    run_jobs()
    with db_factory() as session:
        assert session.scalar(select(Outbox).where(Outbox.request_id == key)).status == 'SUCCESS'
    assert before == len([c for c in peers_double.calls if c[0:2] == ('m1', 'change_room_limit')])


def test_tick_failure_is_reported_and_subsequent_recovery_succeeds(call, peers_double, now):
    from backend.shared.errors import ApiError
    import pytest
    asyncio.run(seed_async())
    peers_double.fail['d', 'audit'] = 'UNAVAILABLE'
    report(call)
    with pytest.raises(ApiError):
        asyncio.run(worker.tick())
    del peers_double.fail['d', 'audit']
    now[0] += timedelta(seconds=5)
    asyncio.run(worker.tick())


def test_success_without_matching_current_limit_is_unknown(call, peers_double):
    def malformed(body):
        return dict(source='M3', businessId=body['businessId'], spaceId=body['spaceId'],
            maxProcessedVersion=body['sourceVersion'], currentLimit=None, queriedRequestId=body['requestId'],
            requestStatus='SUCCESS', message='bad receipt')
    peers_double.responses['m1', 'change_room_limit'] = malformed
    result = mutate(call, 'start_repair', assigned(call), 'technician')
    assert result['seatSync']['status'] == 'UNKNOWN'


def test_unconfigured_timeouts_fail_health_worker_until_seed(call):
    import pytest
    from backend.shared.errors import ApiError
    with pytest.raises(ApiError, match='超时规则尚未初始化'):
        asyncio.run(worker.tick())
    asyncio.run(seed_async())
    asyncio.run(worker.tick())
