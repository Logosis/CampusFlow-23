"""Transactional M3 business rules. No network calls are made while holding row locks."""
import base64
from copy import deepcopy
from datetime import timedelta
import hashlib
import hmac
import json
from uuid import uuid4
from sqlalchemy import func, select, text
from backend.shared.clock import format_time, utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError
from backend.m3.models import Outbox, Receipt, Record, StageCycle, TimeoutConfig, WorkOrder

ACTIVE = ('WAITING_ASSIGNMENT', 'ASSIGNED', 'REPAIRING', 'WAITING_ACCEPTANCE')
STATUSES = (*ACTIVE, 'ACCEPTED', 'CANCELLED')


def uid():
    return uuid4().hex


def sessions():
    return session_factory('m3')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def lock_key(session, key):
    # An absent idempotency row cannot be row-locked; advisory locks protect its insertion.
    number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
    session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': number})


def config(session):
    row = session.get(TimeoutConfig, 1)
    return deepcopy(row.data) if row else {'version': 1, 'checkIntervalSeconds': None, 'rules': []}


def in_scope(scope, order):
    return (scope['type'] == 'GLOBAL'
            or scope['type'] == 'SPACE' and scope['id'] == order.space_id
            or scope['type'] == 'DEPARTMENT' and scope['id'] == order.department_id)


def enter_stage(session, order, stage, now):
    previous = session.scalar(select(StageCycle).where(StageCycle.work_order_id == order.id,
        StageCycle.stage == order.status, StageCycle.exited_at.is_(None))
        .order_by(StageCycle.cycle.desc()).limit(1))
    if previous:
        previous.exited_at = now
    order.status = stage
    order.stage_entered_at = now
    if stage not in ACTIVE:
        return
    cfg = config(session)
    matching = [r for r in cfg['rules'] if r['stage'] == stage and in_scope(r['scope'], order)]
    if len(matching) > 1:
        raise ApiError('CONFLICT', '超时配置存在重叠规则')
    rule = matching[0] if matching else None
    cycle = (session.scalar(select(func.max(StageCycle.cycle)).where(
        StageCycle.work_order_id == order.id, StageCycle.stage == stage)) or 0) + 1
    start = order.created_at if rule and rule['clockStart'] == 'CREATED_AT' else now
    try:
        due = start + timedelta(seconds=rule['timeoutSeconds']) if rule else None
    except OverflowError:
        raise ApiError('INVALID_ARGUMENT', '超时时限超出可表示时间范围') from None
    session.add(StageCycle(id=uid(), work_order_id=order.id, stage=stage, cycle=cycle,
        config_version=cfg['version'], rule=rule,
        due_at=due,
        entered_at=now, escalation_id=None, exited_at=None))
    order.timeout_config_version = cfg['version'] if rule else None


def work_output(w):
    return dict(workOrderId=w.id, reporterId=w.reporter_id, spaceId=w.space_id,
        faultLocation=w.fault_location, description=w.description, status=w.status, version=w.version,
        assigneeId=w.assignee_id, createdAt=format_time(w.created_at), updatedAt=format_time(w.updated_at),
        stageEnteredAt=format_time(w.stage_entered_at),
        firstRepairStartedAt=format_time(w.first_repair_started_at) if w.first_repair_started_at else None,
        acceptedAt=format_time(w.accepted_at) if w.accepted_at else None,
        timeoutConfigVersion=w.timeout_config_version)


def get_order(session, work_id, *, locked=False):
    stmt = select(WorkOrder).where(WorkOrder.id == work_id)
    if locked:
        stmt = stmt.with_for_update()
    order = session.scalar(stmt)
    if order is None:
        raise ApiError('NOT_FOUND', '工单不存在')
    return order


def add_job(session, w, kind, payload, record_id=None, selector=None):
    key = 'm3-' + uid()
    job = Outbox(id=uid(), work_order_id=w.id if w else None, record_id=record_id,
        kind=kind, request_id=key, payload=payload | {'requestId': key}, selector=selector,
        status='PENDING', result=None, message='等待处理', created_at=utc_now(),
        checked_at=None, lease_until=None, attempts=0)
    session.add(job)
    return job


def notify(session, w, record, kind, title, recipients=None, selector=None):
    return add_job(session, w, 'NOTIFICATION', dict(sourceModule='M3', businessId=w.id,
        spaceId=w.space_id, recipientUserIds=list(dict.fromkeys(recipients or [])),
        kind=kind, title=title, body=f'工单 {w.id}：{title}'), record.id, selector)


def seat_job(session, w, action):
    return add_job(session, w, 'SEAT', dict(source='M3', businessId=w.id, spaceId=w.space_id,
        startTime=None, endTime=None, sourceVersion=w.version, action=action,
        businessState='REPAIRING' if action == 'REPAIR' else 'ACCEPTED'))


def record_event(session, w, kind, actor, summary, data=None):
    record = Record(id=uid(), work_order_id=w.id, kind=kind, actor_id=actor,
        occurred_at=utc_now(), version=w.version, summary=summary, data=data or {})
    session.add(record)
    add_job(session, w, 'AUDIT', dict(operation='APPEND', sourceModule='M3',
        actorType='USER' if actor else 'SYSTEM', actorId=actor or 'M3',
        objectType='WORK_ORDER', businessId=w.id, spaceId=w.space_id, action=kind,
        occurredAt=format_time(record.occurred_at), result='SUCCESS', summary=summary), record.id)
    return record


def seat_output(job):
    return dict(requestId=job.request_id, sourceVersion=job.payload['sourceVersion'],
        status=job.status, result=job.result,
        checkedAt=format_time(job.checked_at) if job.checked_at else None, message=job.message)


def notification_output(job, user_id=None):
    result = deepcopy(job.result)
    if result and user_id:
        result['deliveries'] = [d for d in result['deliveries'] if d['recipientUserId'] == user_id]
    return dict(requestId=job.request_id, status=job.status, notification=result, message=job.message)


def latest_seat(session, w):
    jobs = list(session.scalars(select(Outbox).where(Outbox.work_order_id == w.id, Outbox.kind == 'SEAT')))
    return max(jobs, key=lambda j: j.payload['sourceVersion']) if jobs else None


def mutation_output(session, w, record_id, *, report=False):
    session.flush()
    seat = latest_seat(session, w)
    jobs = list(session.scalars(select(Outbox).where(Outbox.work_order_id == w.id,
        Outbox.kind == 'NOTIFICATION', Outbox.record_id == record_id))) if record_id else []
    return dict(workOrder=work_output(w), recordId=None if report else record_id,
        seatSync=seat_output(seat) if seat else None,
        notifications=[notification_output(j) for j in jobs])


def receipt_owner(request):
    return request.state.source_module or request.state.identity['userId']


def existing_receipt(request, function, input):
    """Only call after current authorization, before re-checking old business eligibility."""
    with sessions()() as session:
        receipt = session.scalar(select(Receipt).where(Receipt.owner == receipt_owner(request),
            Receipt.function == function, Receipt.request_id == input.requestId))
        if receipt:
            if receipt.fingerprint != digest(input.model_dump()):
                raise ApiError('REQUEST_ID_REUSED', '原请求编号不能用于不同内容')
            return receipt.id
    return None


def transact(request, function, input, perform):
    """Authorization precedes this call; relation/version checks run inside perform under lock."""
    owner = receipt_owner(request)
    body = input.model_dump()
    with sessions()() as session, session.begin():
        lock_key(session, canonical([owner, function, input.requestId]))
        previous = session.scalar(select(Receipt).where(Receipt.owner == owner,
            Receipt.function == function, Receipt.request_id == input.requestId))
        if previous:
            if previous.fingerprint != digest(body):
                raise ApiError('REQUEST_ID_REUSED', '原请求编号不能用于不同内容')
            return previous.id
        result, work_id = perform(session)
        receipt = Receipt(id=uid(), owner=owner, function=function, request_id=input.requestId,
            fingerprint=digest(body), work_order_id=work_id, response=result)
        session.add(receipt)
        return receipt.id


def render_receipt(receipt_id, user_id=None):
    with sessions()() as session:
        receipt = session.get(Receipt, receipt_id)
        result = deepcopy(receipt.response)
        if 'workOrder' not in result:
            return result
        # Preserve the original business response/version on replay, but show current receipts
        # for precisely the original external requests (never a later operation's sync).
        if result['seatSync']:
            job = session.scalar(select(Outbox).where(Outbox.request_id == result['seatSync']['requestId']))
            result['seatSync'] = seat_output(job)
        result['notifications'] = [notification_output(session.scalar(select(Outbox).where(
            Outbox.request_id == n['requestId'])), user_id) for n in result['notifications']]
        return result


def expect(w, version, stages):
    if w.version != version:
        raise ApiError('CONFLICT', '工单版本已变化，请刷新')
    if w.status not in stages:
        raise ApiError('CONFLICT', '当前阶段不允许此操作')


def paginate(items, input, namespace, secret, key):
    filters = input.model_dump(exclude={'cursor', 'limit'})
    signature_context = digest([namespace, filters])
    after = None
    if input.cursor:
        try:
            encoded, signature = input.cursor.split('.')
            if not hmac.compare_digest(signature, hmac.new(secret.encode(), encoded.encode(), 'sha256').hexdigest()):
                raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            if payload['context'] != signature_context or not isinstance(payload['after'], str):
                raise ValueError()
            after = payload['after']
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ApiError('INVALID_ARGUMENT', '分页游标无效或筛选条件已变化') from None
    rows = sorted(items, key=key)
    if after is not None:
        rows = [row for row in rows if key(row) > after]
    selected = rows[:input.limit]
    cursor = None
    if len(rows) > input.limit:
        encoded = base64.urlsafe_b64encode(canonical({'context': signature_context,
            'after': key(selected[-1])}).encode()).decode().rstrip('=')
        cursor = encoded + '.' + hmac.new(secret.encode(), encoded.encode(), 'sha256').hexdigest()
    return {'items': selected, 'nextCursor': cursor}
