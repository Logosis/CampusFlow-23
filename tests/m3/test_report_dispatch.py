from uuid import uuid4
import pytest
from sqlalchemy import func, select
from backend.m3.models import WorkOrder, Record, Outbox


def report(call, **values):
    return call('report_fault', dict(spaceId='sp-school', faultLocation='门口灯具', description='灯不亮', requestId=uuid4().hex) | values)


def test_report_unique_request_and_validation(call, db_factory, peers_double):
    body = dict(spaceId='sp-school', faultLocation='门口', description='故障', requestId='one')
    first = call('report_fault', body)
    assert first['workOrder']['status'] == 'WAITING_ASSIGNMENT'
    assert first['workOrder']['reporterId'] == 'u-student1'
    assert first['workOrder']['version'] == 1
    assert first['recordId'] is None and first['seatSync'] is None and first['notifications'] == []
    assert call('report_fault', body) == first
    assert call('report_fault', body | {'description': 'changed'}, status=409)['code'] == 'REQUEST_ID_REUSED'
    call('report_fault', body | {'description': '  '}, status=422)
    call('report_fault', body | {'reporterId': 'u-admin'}, status=422)
    call('report_fault', body, token='technician', status=403)
    call('report_fault', body | {'spaceId': 'missing', 'requestId': 'missing-space'}, status=404)
    peers_double.spaces[0]['registered'] = False
    call('report_fault', body | {'requestId': 'unregistered'}, status=422)
    with db_factory() as session:
        assert session.scalar(select(func.count()).select_from(WorkOrder)) == 1
        assert session.scalar(select(func.count()).select_from(Record)) == 1
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == 'SEAT')) == 0


def test_locations_scope_and_cursor(call, peers_double):
    body = {'limit': 1}
    first = call('get_report_locations', body)
    assert len(first['items']) == 1 and first['nextCursor']
    second = call('get_report_locations', body | {'cursor': first['nextCursor']})
    assert first['items'][0]['spaceId'] != second['items'][0]['spaceId']
    call('get_report_locations', body | {'cursor': 'invalid'}, status=422)
    call('get_report_locations', body | {'cursor': first['nextCursor'], 'departmentId': 'dep-cs'}, status=422)
    peers_double.grants['student1'] = [{'permissionCode': 'm3.work_order.report', 'scope': {'type': 'SPACE', 'id': 'sp-cs'}}]
    assert [s['spaceId'] for s in call('get_report_locations')['items']] == ['sp-cs']
    call('get_report_locations', token='supervisor', status=403)


def test_dispatch_candidates_permission_versions_and_replay(call, db_factory):
    w = report(call)['workOrder']
    body = {'workOrderId': w['workOrderId']}
    candidates = call('get_dispatch_candidates', body, 'dispatcher')['items']
    assert 'u-technician' in [p['userId'] for p in candidates]
    call('get_dispatch_candidates', body, status=403)
    call('get_dispatch_tasks', {}, status=403)
    assert call('get_dispatch_tasks', body, 'dispatcher')['items'][0]['workOrder'] == w
    mutation = body | dict(assigneeId='u-technician', expectedVersion=1, requestId='dispatch')
    call('dispatch_order', mutation, status=403)
    call('dispatch_order', mutation | {'assigneeId': 'u-student1'}, 'dispatcher', status=403)
    call('dispatch_order', mutation | {'assigneeId': 'missing'}, 'dispatcher', status=404)
    call('dispatch_order', mutation | {'expectedVersion': 2}, 'dispatcher', status=409)
    result = call('dispatch_order', mutation, 'dispatcher')
    assert result['workOrder']['status'] == 'ASSIGNED' and result['workOrder']['version'] == 2
    assert result['seatSync'] is None and len(result['notifications']) == 1
    assert call('dispatch_order', mutation, 'dispatcher') == result
    call('dispatch_order', mutation | {'requestId': 'another'}, 'dispatcher', status=409)
    call('get_dispatch_candidates', body, 'dispatcher', status=409)
    assert call('get_dispatch_tasks', {}, 'dispatcher')['items'] == []
    with db_factory() as session:
        assert session.scalar(select(func.count()).select_from(Record).where(Record.kind == 'DISPATCH')) == 1


@pytest.mark.parametrize('function', ['get_report_locations', 'report_fault', 'get_dispatch_tasks', 'get_dispatch_candidates', 'dispatch_order'])
def test_no_auth(call, function):
    call(function, {}, token=None, status=401)


def test_dependency_failures_are_not_empty_success(call, peers_double):
    peers_double.fail['d', 'query_master'] = 'UNAVAILABLE'
    call('get_report_locations', status=503)
    call('report_fault', dict(spaceId='sp-school', faultLocation='a', description='b', requestId='x'), status=503)
    peers_double.fail['d', 'check_permission'] = 'TRANSPORT'
    call('get_dispatch_tasks', {}, 'dispatcher', status=503)


def test_replays_check_current_permission_but_not_old_business_eligibility(call, peers_double):
    body = dict(spaceId='sp-school', faultLocation='门口', description='故障', requestId='original')
    result = call('report_fault', body)
    peers_double.spaces[0]['registered'] = False
    assert call('report_fault', body) == result
    peers_double.grants['student1'] = []
    call('report_fault', body, status=403)
    peers_double.spaces[0]['registered'] = True
    peers_double.grant('student1', 'm3.work_order.report')
    w = report(call)['workOrder']
    request = dict(workOrderId=w['workOrderId'], assigneeId='u-technician', expectedVersion=1, requestId='assign')
    dispatched = call('dispatch_order', request, 'dispatcher')
    peers_double.users['technician']['accountStatus'] = 'DISABLED'
    assert call('dispatch_order', request, 'dispatcher') == dispatched
    peers_double.grants['dispatcher'] = []
    call('dispatch_order', request, 'dispatcher', status=403)


@pytest.mark.parametrize('function,body,token', [
    ('get_report_locations', {'cursor': None}, 'student1'),
    ('get_dispatch_tasks', {'spaceId': None}, 'dispatcher'),
    ('get_work_tasks', {'purpose': 'REPAIR', 'workOrderId': None}, 'technician'),
    ('get_work_orders', {'view': 'OWN', 'status': None}, 'admin'),
    ('get_maintenance_stats', {'startTime': '2026-10-14T00:00:00Z', 'endTime': '2026-10-15T00:00:00Z', 'spaceId': None}, 'admin'),
])
def test_optional_fields_omitted_not_null(call, function, body, token):
    call(function, body, token, status=422)


def test_permission_subject_and_people_contract_mismatch_are_unavailable(call, peers_double):
    peers_double.responses['d', 'check_permission'] = dict(subjectType='USER', subjectId='u-admin',
        allowed=True, allowedScopes=[{'type': 'GLOBAL', 'id': None}], reason='bad subject')
    call('get_report_locations', status=503)
    del peers_double.responses['d', 'check_permission']
    w = report(call)['workOrder']
    peers_double.responses['d', 'query_people'] = dict(items=[dict(userId='u-student1', displayName='student1',
        departmentId='dep-cs', accountStatus='ACTIVE', eligible=True, matchedPermissionCodes=[])], nextCursor=None)
    call('get_dispatch_candidates', {'workOrderId': w['workOrderId']}, 'dispatcher', status=503)
