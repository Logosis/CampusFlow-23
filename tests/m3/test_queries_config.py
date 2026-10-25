import asyncio
from datetime import timedelta
from sqlalchemy import select
from backend.m3 import worker
from backend.m3.models import StageCycle
from backend.m3.seed import seed_async
from tests.m3.conftest import FIXTURE
from tests.m3.test_lifecycle import assigned, mutate
from tests.m3.test_report_dispatch import report


def test_read_views_detail_redaction_and_scope(call, peers_double):
    peers_double.grant('student1', 'm3.work_order.read')
    peers_double.grant('student2', 'm3.work_order.read')
    # 本用例测试局部授权；先移除新共同初始化中的管理员全局查询授权。
    peers_double.grants['admin'] = [g for g in peers_double.grants['admin']
                                   if g['permissionCode'] != 'm3.work_order.read_all']
    peers_double.grant('admin', 'm3.work_order.read_all', {'type': 'SPACE', 'id': 'sp-school'})
    w = assigned(call)
    own = call('get_work_orders', {'view': 'OWN'})
    assert own['items'][0]['workOrderId'] == w['workOrderId']
    call('get_work_orders', {'view': 'MANAGED'}, status=403)
    call('get_work_order', {'workOrderId': w['workOrderId']}, 'student2', status=403)
    detail = call('get_work_order', {'workOrderId': w['workOrderId']})
    assert detail['detail']['assignments'] == []
    assert [e['type'] for e in detail['timeline']] == ['REPORT', 'DISPATCH']
    assert detail['timeline'][1]['actorId'] is None
    detail = call('get_work_order', {'workOrderId': w['workOrderId']}, 'admin')
    assert len(detail['detail']['assignments']) == 1
    report(call, spaceId='sp-cs')
    assert len(call('get_work_orders', {'view': 'MANAGED'}, 'admin')['items']) == 1
    call('get_work_orders', {'view': 'OWN', 'startTime': '2026-10-14T00:00:00Z'}, status=422)
    call('get_work_order', {'workOrderId': 'missing'}, status=404)


def test_statistics_created_cohort_cancellations_overdue_dedup_and_duration(call, peers_double, now):
    peers_double.grant('admin', 'm3.statistics.read')
    peers_double.grant('student1', 'm3.work_order.cancel')
    asyncio.run(seed_async())
    start = now[0]
    w = assigned(call)
    w = mutate(call, 'start_repair', w, 'technician')['workOrder']
    now[0] += timedelta(hours=24)
    worker._scan_due()
    completed = mutate(call, 'submit_completion', w, 'technician', summary='done')
    now[0] += timedelta(hours=24)
    worker._scan_due()
    mutate(call, 'submit_acceptance', completed['workOrder'], 'inspector', completionId=completed['recordId'], decision='PASS')
    cancelled = report(call)['workOrder']
    mutate(call, 'cancel_work_order', cancelled, 'student1')
    body = {'startTime': start.isoformat(), 'endTime': (now[0] + timedelta(seconds=1)).isoformat()}
    result = call('get_maintenance_stats', body, 'admin')
    assert result['totalCount'] == 2 and result['statusCounts']['ACCEPTED'] == 1 and result['statusCounts']['CANCELLED'] == 1
    assert result['overdueCount'] == 1 and result['sampleCount'] == 1 and result['averageRepairSeconds'] == 172800.0
    call('get_maintenance_stats', body, status=403)
    call('get_maintenance_stats', body | {'endTime': body['startTime']}, 'admin', status=422)
    empty = call('get_maintenance_stats', body | {'spaceId': 'sp-cs'}, 'admin')
    assert empty['totalCount'] == 0 and empty['sampleCount'] == 0 and empty['averageRepairSeconds'] is None


def test_config_only_m1_user_validate_replay_and_snapshot(call, now, db_factory):
    asyncio.run(seed_async())
    w = report(call)['workOrder']
    original = call('get_timeout_config', {}, 'admin', source='m1')
    call('get_timeout_config', {}, 'admin', status=403)
    call('get_timeout_config', {}, 'student1', source='m1', status=403)
    call('get_timeout_config', {}, token=None, source='m1', status=403)
    body = dict(checkIntervalSeconds=60, rules=original['rules'], expectedVersion=1, requestId='cfg')
    body['rules'][0]['timeoutSeconds'] = 60
    updated = call('update_timeout_config', body, 'admin', source='m1')
    assert updated['version'] == 2
    assert call('update_timeout_config', body, 'admin', source='m1') == updated
    call('update_timeout_config', body | {'checkIntervalSeconds': 5}, 'admin', source='m1', status=409)
    call('update_timeout_config', body | {'requestId': 'old'}, 'admin', source='m1', status=409)
    call('update_timeout_config', body | {'requestId': 'zero', 'checkIntervalSeconds': 0}, 'admin', source='m1', status=422)
    duplicate = [body['rules'][0], body['rules'][0] | {'ruleId': 'overlap'}]
    call('update_timeout_config', body | {'rules': duplicate, 'expectedVersion': 2, 'requestId': 'overlap'}, 'admin', source='m1', status=422)
    unknown = [body['rules'][0] | {'recipientPermissionCodes': ['unknown.permission']}]
    call('update_timeout_config', body | {'rules': unknown, 'expectedVersion': 2, 'requestId': 'unknown'}, 'admin', source='m1', status=422)
    with db_factory() as s:
        snapshot = s.scalar(select(StageCycle).where(StageCycle.work_order_id == w['workOrderId']))
        assert snapshot.config_version == 1 and snapshot.rule['timeoutSeconds'] == 86400
    now[0] += timedelta(minutes=2)
    worker._scan_due()
    from backend.m3.models import Record
    with db_factory() as s:
        assert s.scalar(select(Record.id).where(Record.kind == 'ESCALATE')) is None


def test_management_uses_current_d_scope_after_space_department_changed(call, peers_double):
    peers_double.grants['admin'] = []
    for code in ('m3.work_order.read', 'm3.work_order.read_all'):
        peers_double.grant('admin', code, {'type': 'DEPARTMENT', 'id': 'dep-cs'})
    w = report(call, spaceId='sp-cs')['workOrder']
    assert len(call('get_work_orders', {'view': 'MANAGED'}, 'admin')['items']) == 1
    peers_double.spaces[1]['departmentId'] = 'dep-ee'
    assert call('get_work_orders', {'view': 'MANAGED'}, 'admin')['items'] == []
    call('get_work_order', {'workOrderId': w['workOrderId']}, 'admin', status=403)


def test_overdue_statistics_include_expired_stage_before_polling(call, now, peers_double):
    peers_double.grant('admin', 'm3.statistics.read')
    peers_double.grant('student1', 'm3.work_order.cancel')
    asyncio.run(seed_async())
    start = now[0]
    w = report(call)['workOrder']
    now[0] += timedelta(hours=24, seconds=1)
    mutate(call, 'cancel_work_order', w, 'student1')
    # No timeout scan was run: elapsed stage time still counts, including a cancelled order.
    body = dict(startTime=start.isoformat(), endTime=(now[0] + timedelta(seconds=1)).isoformat())
    result = call('get_maintenance_stats', body, 'admin')
    assert result['overdueCount'] == 1 and result['statusCounts']['CANCELLED'] == 1
    w = report(call)['workOrder']
    mutate(call, 'cancel_work_order', w, 'student1')
    now[0] += timedelta(hours=25)
    result = call('get_maintenance_stats', body, 'admin')
    assert result['totalCount'] == 2 and result['overdueCount'] == 1  # early cancellation cannot age into overdue
