"""Durable, leased compensation and phase escalation; restarts retain original request IDs."""
from datetime import timedelta
import logging
from sqlalchemy import select
from backend.m3 import domain as db, peers
from backend.m3.models import Outbox, Record, StageCycle, WorkOrder, WorkerState
from backend.m3.schemas import LimitResult, Notification
from backend.shared.clock import format_time
from backend.shared.errors import ApiError

logger = logging.getLogger(__name__)


def _notification(result, payload):
    value = peers.checked(Notification, result)
    for key in ('requestId', 'sourceModule', 'businessId', 'spaceId', 'kind', 'title', 'body'):
        if value[key] != payload[key]:
            raise ApiError('UNAVAILABLE', '通知回执与原请求不一致')
    if {d['recipientUserId'] for d in value['deliveries']} != set(payload['recipientUserIds']):
        raise ApiError('UNAVAILABLE', '通知收件人回执不一致')
    if len(value['deliveries']) != len(payload['recipientUserIds']):
        raise ApiError('UNAVAILABLE', '通知投递回执存在重复收件人')
    return value


def _limit(result, payload, *, writing=False):
    value = peers.checked(LimitResult, result)
    if (value['source'] != 'M3' or value['businessId'] != payload['businessId']
            or value['queriedRequestId'] != payload['requestId']
            or value['spaceId'] not in (None, payload['spaceId'])):
        raise ApiError('UNAVAILABLE', '下架回执与原请求不一致')
    if writing and value['requestStatus'] != 'SUCCESS':
        raise ApiError('UNAVAILABLE', '写回未确认原请求成功')
    limit = value['currentLimit']
    if limit and (limit['source'] != 'M3' or limit['businessId'] != payload['businessId']
                  or limit['spaceId'] != payload['spaceId']):
        raise ApiError('UNAVAILABLE', '限制记录与本工单不一致')
    if value['requestStatus'] == 'SUCCESS' and (
            value['maxProcessedVersion'] is None or value['maxProcessedVersion'] < payload['sourceVersion']):
        raise ApiError('UNAVAILABLE', '成功回执缺少已处理版本')
    if writing and (limit is None or limit['sourceVersion'] != payload['sourceVersion']
                    or limit['state'] != ('ACTIVE' if payload['action'] == 'REPAIR' else 'RELEASED')):
        raise ApiError('UNAVAILABLE', '写回缺少本次操作的限制状态')
    return value


def _audit(result, payload):
    if (not isinstance(result, dict) or not isinstance(result.get('auditId'), str)
            or not result['auditId'] or any(result.get(k) != v for k, v in payload.items() if k != 'operation')
            or result.get('verifiedBy') not in ('D', 'M3')):
        raise ApiError('UNAVAILABLE', '审计回执与原请求不一致')
    return result


def _claim(job_id):
    now = db.utc_now()
    with db.sessions()() as session, session.begin():
        job = session.scalar(select(Outbox).where(Outbox.id == job_id).with_for_update(skip_locked=True))
        if job is None or job.status in ('SUCCESS', 'REJECTED'):
            return None
        if job.status == 'ACCEPTED' and job.result and all(
                d['status'] in ('SUBMITTED', 'FAILED', 'UNKNOWN') for d in job.result['deliveries']):
            return None
        if job.lease_until and job.lease_until > now:
            return None
        previous = job.status
        job.lease_until = now + timedelta(seconds=30)
        job.attempts += 1
        # Persist uncertainty BEFORE external mutation; process death must not make it unsent.
        if previous == 'PENDING' and job.selector is None:
            job.status = 'UNKNOWN'
            job.message = '请求处理中，等待原编号回执'
        session.flush()
        return dict(id=job.id, kind=job.kind, payload=job.payload.copy(), selector=job.selector,
            previous=previous, lease=job.lease_until, work_id=job.work_order_id)


def _finish(claim, status, result, message, checked=False):
    with db.sessions()() as session, session.begin():
        job = session.scalar(select(Outbox).where(Outbox.id == claim['id']).with_for_update())
        if job.lease_until != claim['lease']:
            return
        job.status, job.message = status, message
        if result is not None:
            job.result = result
        if checked:
            job.checked_at = db.utc_now()
        job.lease_until = None


async def _process(claim):
    payload, kind = claim['payload'], claim['kind']
    phase = 'resolve' if claim['selector'] else 'query' if claim['previous'] != 'PENDING' else 'write'
    try:
        if claim['selector']:
            selector = claim['selector']
            rows = await peers.people(payload['spaceId'], selector['purpose'], selector['permissionCodes'])
            recipients = sorted({p['userId'] for p in rows} - set(selector.get('excludeUserIds', [])))
            if not recipients:
                _finish(claim, 'PENDING', None, '尚无合格收件人，将继续核对')
                return True
            payload = payload | {'recipientUserIds': recipients}
            with db.sessions()() as session, session.begin():
                job = session.scalar(select(Outbox).where(Outbox.id == claim['id']).with_for_update())
                if job.lease_until != claim['lease']:
                    return False
                job.payload, job.selector, job.status = payload, None, 'UNKNOWN'
            phase = 'write'
        if kind == 'SEAT':
            if phase == 'query':
                result = _limit(await peers.peer('m1', 'get_room_limit', dict(source='M3',
                    businessId=payload['businessId'], requestId=payload['requestId'])), payload)
                if result['requestStatus'] in ('SUCCESS', 'REJECTED'):
                    _finish(claim, result['requestStatus'], result, result['message'], True)
                    return False
                # Do not replay an obsolete REPAIR after a higher-version RELEASE exists.
                with db.sessions()() as session:
                    w = db.get_order(session, payload['businessId'])
                    newest = db.latest_seat(session, w)
                    if newest.payload['sourceVersion'] > payload['sourceVersion']:
                        _finish(claim, 'UNKNOWN', result, '旧请求结果未确认，已有更高版本限制请求', True)
                        return True
            phase = 'write'
            result = _limit(await peers.peer('m1', 'change_room_limit', payload), payload, writing=True)
            _finish(claim, 'SUCCESS', result, result['message'], True)
        elif kind == 'NOTIFICATION':
            if phase == 'query':
                result = _notification(await peers.peer('d', 'get_notification', {'requestId': payload['requestId']}), payload)
            else:
                result = _notification(await peers.peer('d', 'send_notification', payload), payload)
            _finish(claim, 'ACCEPTED', result, 'D已受理，投递结果见deliveries', True)
        elif kind == 'AUDIT':
            if phase == 'query':
                try:
                    result = _audit(await peers.peer('d', 'audit', dict(operation='GET', requestId=payload['requestId'])), payload)
                    _finish(claim, 'SUCCESS', result, '审计已记录', True)
                    return False
                except ApiError as exc:
                    if exc.code != 'NOT_FOUND':
                        raise
            phase = 'write'
            result = _audit(await peers.peer('d', 'audit', payload), payload)
            _finish(claim, 'SUCCESS', result, '审计已记录', True)
        else:
            raise RuntimeError('未知M3待处理任务')
        return False
    except ApiError as exc:
        # A failed query says nothing about the original mutation; keep its uncertainty.
        rejected = phase == 'write' and exc.code != 'UNAVAILABLE'
        status = 'REJECTED' if rejected else 'PENDING' if phase == 'resolve' else claim['previous']
        if status not in ('ACCEPTED', 'PENDING', 'REJECTED'):
            status = 'UNKNOWN'
        if phase == 'write' and not rejected:
            status = 'UNKNOWN'
        _finish(claim, status, None, '依赖调用未完成：' + exc.code, rejected)
        logger.warning('M3 %s requestId=%s phase=%s code=%s', kind, payload['requestId'], phase, exc.code)
        return not rejected
    except Exception:
        # Never log peer payloads, credentials or database connection strings.
        _finish(claim, 'UNKNOWN', None, '本地处理异常，等待原编号核实')
        logger.error('M3 %s requestId=%s 本地处理异常', kind, payload['requestId'])
        raise


async def process_jobs(work_id=None):
    with db.sessions()() as session:
        stmt = select(Outbox.id).where(Outbox.status.in_(('PENDING', 'UNKNOWN', 'ACCEPTED'))).order_by(Outbox.created_at, Outbox.id)
        if work_id:
            stmt = stmt.where(Outbox.work_order_id == work_id)
        ids = list(session.scalars(stmt))
    failed = False
    for ident in ids:
        claim = _claim(ident)
        if claim:
            failed = await _process(claim) or failed
    return failed


def _scan_due():
    now = db.utc_now()
    with db.sessions()() as session, session.begin():
        db.lock_key(session, 'm3:timeout-scan')
        cfg = db.config(session)
        state = session.get(WorkerState, 'timeout')
        if not state:
            state = WorkerState(name='timeout', last_run=None)
            session.add(state)
        interval = cfg['checkIntervalSeconds']
        if interval is None or state.last_run and now < state.last_run + timedelta(seconds=interval):
            return
        # WorkOrder lock serializes timeout against acceptance/cancel/state transitions.
        orders = list(session.scalars(select(WorkOrder).where(WorkOrder.status.in_(db.ACTIVE))
            .order_by(WorkOrder.id).with_for_update()))
        for w in orders:
            cycle = session.scalar(select(StageCycle).where(StageCycle.work_order_id == w.id,
                StageCycle.stage == w.status).order_by(StageCycle.cycle.desc()).limit(1))
            if not cycle or cycle.due_at is None or cycle.due_at > now or cycle.escalation_id:
                continue
            event = db.record_event(session, w, 'ESCALATE', None, '当前阶段超时，已发起升级通知')
            cycle.escalation_id = event.id
            event.data = dict(escalationId=event.id, workOrderId=w.id, stage=w.status,
                cycle=cycle.cycle, configVersion=cycle.config_version, dueAt=format_time(cycle.due_at),
                triggeredAt=format_time(now), action='NOTIFY')
            db.notify(session, w, event, 'M3_TIMEOUT_ESCALATED', '工单阶段超时',
                selector=dict(purpose='ESCALATION', permissionCodes=cycle.rule['recipientPermissionCodes']))
        state.last_run = now


def _compensation_due():
    now = db.utc_now()
    with db.sessions()() as session, session.begin():
        db.lock_key(session, 'm3:compensation')
        state = session.get(WorkerState, 'compensation')
        if not state:
            state = WorkerState(name='compensation', last_run=None)
            session.add(state)
        if state.last_run and now < state.last_run + timedelta(seconds=5):
            return False
        state.last_run = now
        return True


async def tick():
    _scan_due()
    if _compensation_due() and await process_jobs():
        raise ApiError('UNAVAILABLE', 'M3存在未确认的同步、通知或审计，请查看模块日志与原请求记录')
