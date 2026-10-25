from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import func, select
from backend.m3.models import Record, WorkOrder, Outbox
from tests.m3.test_report_dispatch import report


def assigned(call):
    w = report(call)['workOrder']
    return call('dispatch_order', dict(workOrderId=w['workOrderId'], assigneeId='u-technician',
        expectedVersion=w['version'], requestId=uuid4().hex), 'dispatcher')['workOrder']


def mutate(call, fn, w, token, **extra):
    return call(fn, dict(workOrderId=w['workOrderId'], expectedVersion=w['version'], requestId=uuid4().hex) | extra, token)


def test_full_lifecycle_return_preserves_first_start_and_final_release(call, now, peers_double):
    w = assigned(call)
    started = mutate(call, 'start_repair', w, 'technician')
    w = started['workOrder']
    assert w['status'] == 'REPAIRING' and started['seatSync']['status'] == 'SUCCESS'
    assert started['seatSync']['result']['currentLimit']['state'] == 'ACTIVE'
    first = w['firstRepairStartedAt']
    now[0] += timedelta(hours=1)
    completed = mutate(call, 'submit_completion', w, 'technician', summary='第一次修复')
    assert completed['workOrder']['status'] == 'WAITING_ACCEPTANCE'
    assert completed['seatSync']['result']['currentLimit']['state'] == 'ACTIVE'
    tasks = call('get_work_tasks', {'purpose': 'ACCEPTANCE', 'workOrderId': w['workOrderId']}, 'inspector')['items']
    assert tasks[0]['completions'][-1]['completionId'] == completed['recordId']
    returned = mutate(call, 'submit_acceptance', completed['workOrder'], 'inspector',
        completionId=completed['recordId'], decision='FAIL', comment='仍有问题')
    assert returned['workOrder']['status'] == 'REPAIRING'
    assert returned['workOrder']['firstRepairStartedAt'] == first
    assert returned['seatSync']['result']['currentLimit']['state'] == 'ACTIVE'
    now[0] += timedelta(hours=2)
    completed2 = mutate(call, 'submit_completion', returned['workOrder'], 'technician', summary='返修完成')
    call('submit_acceptance', dict(workOrderId=w['workOrderId'], expectedVersion=completed2['workOrder']['version'],
        requestId='old-completion', completionId=completed['recordId'], decision='PASS'), 'inspector', status=409)
    accepted = mutate(call, 'submit_acceptance', completed2['workOrder'], 'inspector',
        completionId=completed2['recordId'], decision='PASS')
    assert accepted['workOrder']['status'] == 'ACCEPTED'
    assert accepted['seatSync']['status'] == 'SUCCESS'
    assert accepted['seatSync']['result']['currentLimit']['state'] == 'RELEASED'
    call('submit_evaluation', {'workOrderId': w['workOrderId'], 'requestId': 'other', 'content': '好'}, 'student2', status=403)
    body = {'workOrderId': w['workOrderId'], 'requestId': 'evaluation', 'content': '修好了'}
    evaluated = call('submit_evaluation', body)
    assert evaluated['workOrder']['status'] == 'ACCEPTED'
    assert call('submit_evaluation', body) == evaluated
    call('submit_evaluation', body | {'requestId': 'twice'}, status=409)
    call('submit_evaluation', body | {'content': 'changed'}, status=409)
    calls = [b for target, fn, b, *_ in peers_double.calls if target == 'm1' and fn == 'change_room_limit']
    assert [b['action'] for b in calls] == ['REPAIR', 'RELEASE']
    assert calls[1]['sourceVersion'] > calls[0]['sourceVersion']
    assert all(b['source'] == 'M3' and b['businessId'] == w['workOrderId'] for b in calls)


def test_relations_permissions_empty_text_and_versions(call, peers_double):
    w = assigned(call)
    base = dict(workOrderId=w['workOrderId'], expectedVersion=w['version'], requestId='start')
    call('start_repair', base, status=403)
    call('start_repair', base, 'admin', status=403)
    call('start_repair', base | {'expectedVersion': 99}, 'technician', status=409)
    started = call('start_repair', base, 'technician')
    assert call('start_repair', base, 'technician') == started
    call('submit_completion', dict(workOrderId=w['workOrderId'], expectedVersion=3, requestId='c', summary=' '), 'technician', status=422)
    completed = mutate(call, 'submit_completion', started['workOrder'], 'technician', summary='done')
    peers_double.grant('technician', 'm3.acceptance.manage')
    call('submit_acceptance', dict(workOrderId=w['workOrderId'], expectedVersion=4, requestId='self',
        completionId=completed['recordId'], decision='PASS'), 'technician', status=403)
    call('submit_evaluation', dict(workOrderId=w['workOrderId'], requestId='early', content='好'), status=409)
    call('get_work_tasks', {'purpose': 'REPAIR'}, status=403)
    assert call('get_work_tasks', {'purpose': 'REPAIR'}, 'technician')['items'] == []


def test_cancel_reporter_only_before_repair_and_replays(call, peers_double):
    for account in ('student1', 'student2'):
        peers_double.grant(account, 'm3.work_order.cancel')
    w = assigned(call)
    body = dict(workOrderId=w['workOrderId'], expectedVersion=2, requestId='cancel', reason='重复报修')
    call('cancel_work_order', body, 'student2', status=403)
    cancelled = call('cancel_work_order', body)
    assert cancelled['workOrder']['status'] == 'CANCELLED' and cancelled['seatSync'] is None
    assert call('cancel_work_order', body) == cancelled
    call('start_repair', dict(workOrderId=w['workOrderId'], expectedVersion=2, requestId='late'), 'technician', status=409)
    w = assigned(call)
    start = mutate(call, 'start_repair', w, 'technician')
    call('cancel_work_order', dict(workOrderId=w['workOrderId'], expectedVersion=3, requestId='after'), status=409)
    assert start['workOrder']['status'] == 'REPAIRING'


def test_start_cancel_concurrent_only_one_wins(api, call, peers_double, db_factory):
    peers_double.grant('student1', 'm3.work_order.cancel')
    w = assigned(call)
    def invoke(fn, token):
        return api.post('/api/v1/m3/' + fn, headers={'Authorization': 'Bearer ' + token},
            json=dict(workOrderId=w['workOrderId'], expectedVersion=2, requestId=fn)).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: invoke(*args), [('start_repair', 'technician'), ('cancel_work_order', 'student1')]))
    assert sorted(results) == [200, 409]
    with db_factory() as s:
        row = s.get(WorkOrder, w['workOrderId'])
        assert row.version == 3
        assert s.scalar(select(func.count()).select_from(Record).where(Record.kind.in_(('REPAIR', 'CANCEL')))) == 1
        assert s.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == 'SEAT')) == (row.status == 'REPAIRING')


def test_pass_fail_concurrent_exactly_one_decision(api, call, db_factory):
    w = mutate(call, 'start_repair', assigned(call), 'technician')['workOrder']
    completed = mutate(call, 'submit_completion', w, 'technician', summary='done')
    body = dict(workOrderId=w['workOrderId'], expectedVersion=4, completionId=completed['recordId'])
    def invoke(decision):
        return api.post('/api/v1/m3/submit_acceptance', headers={'Authorization': 'Bearer inspector'},
            json=body | {'decision': decision, 'requestId': decision}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ['PASS', 'FAIL']))
    assert sorted(results) == [200, 409]
    with db_factory() as s:
        assert s.scalar(select(func.count()).select_from(Record).where(Record.kind.in_(('ACCEPT', 'RETURN')))) == 1
