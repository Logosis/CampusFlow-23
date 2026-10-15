from sqlalchemy import select
from backend.m3 import domain as db
from backend.m3 import peers
from backend.m3.models import Record, Outbox, WorkOrder
from backend.m3.schemas import Space
from backend.shared.errors import ApiError
from backend.shared.clock import format_time, utc_now


def user(request):
    return request.state.identity['userId']


def secret(request):
    value = request.app.state.settings.credentials.get('m3')
    if not value:
        raise ApiError('UNAVAILABLE', '模块凭据未配置')
    return value


def order_snapshot(work_id):
    with db.sessions()() as session:
        return db.get_order(session, work_id)


async def authorize_order(request, work_id, code):
    w = order_snapshot(work_id)
    await peers.permission(code, request, w.space_id)
    return w


def paged(items, input, request, namespace, key):
    return db.paginate(items, input, namespace + ':' + user(request), secret(request), key)


def is_eligible(person):
    return person['accountStatus'] == 'ACTIVE' and person['eligible'] is True


async def locations(request, input):
    scopes = await peers.permission('m3.work_order.report', request)
    if input.spaceId:
        spaces = [await peers.get_space(input.spaceId, request)]
    else:
        body = {'operation': 'LIST_SPACES', 'isStudyRoom': True, 'includeUnregistered': False}
        if input.departmentId:
            body['departmentId'] = input.departmentId
        spaces = await peers.collect('d', 'query_master', body, Space, request)
    spaces = [s for s in spaces if s['registered'] and s['isStudyRoom']
              and (not input.departmentId or s['departmentId'] == input.departmentId)
              and any(scope['type'] == 'GLOBAL'
                  or scope['type'] == 'SPACE' and scope['id'] == s['spaceId']
                  or scope['type'] == 'DEPARTMENT' and scope['id'] == s['departmentId'] for scope in scopes)]
    return paged(spaces, input, request, 'locations', lambda s: s['spaceId'])


async def report(request, input):
    await peers.permission('m3.work_order.report', request, input.spaceId)
    space = await peers.get_space(input.spaceId, request)
    if not space['registered'] or not space['isStudyRoom']:
        raise ApiError('INVALID_ARGUMENT', '只能上报登记中的自习教室')

    def perform(session):
        now = utc_now()
        w = WorkOrder(id=db.uid(), reporter_id=user(request), space_id=space['spaceId'],
            department_id=space['departmentId'], fault_location=input.faultLocation,
            description=input.description, status='WAITING_ASSIGNMENT', version=1,
            assignee_id=None, created_at=now, updated_at=now, stage_entered_at=now,
            first_repair_started_at=None, accepted_at=None, timeout_config_version=None)
        session.add(w)
        session.flush()
        db.enter_stage(session, w, 'WAITING_ASSIGNMENT', now)
        event = db.record_event(session, w, 'REPORT', user(request), '故障已上报')
        return db.mutation_output(session, w, event.id, report=True), w.id

    receipt = db.transact(request, 'report_fault', input, perform)
    return db.render_receipt(receipt, user(request))


async def candidates(request, input):
    w = await authorize_order(request, input.workOrderId, 'm3.dispatch.manage')
    if w.status != 'WAITING_ASSIGNMENT':
        raise ApiError('CONFLICT', '当前工单不在待派单阶段')
    people = await peers.people(w.space_id, 'DISPATCH', ['m3.repair.manage'], request)
    return paged(people, input, request, 'dispatch-candidates', lambda p: p['userId'])


async def dispatch(request, input):
    w = await authorize_order(request, input.workOrderId, 'm3.dispatch.manage')
    people = await peers.people(w.space_id, 'DISPATCH', ['m3.repair.manage'], request, [input.assigneeId])
    if not is_eligible(people[0]):
        raise ApiError('FORBIDDEN', '承接人不具备该教室维修资格')

    def perform(session):
        w = db.get_order(session, input.workOrderId, locked=True)
        db.expect(w, input.expectedVersion, ('WAITING_ASSIGNMENT',))
        w.assignee_id = input.assigneeId
        w.version += 1
        w.updated_at = utc_now()
        db.enter_stage(session, w, 'ASSIGNED', w.updated_at)
        event = db.record_event(session, w, 'DISPATCH', user(request), '已派发给维修人员')
        event.data = dict(assignmentId=event.id, workOrderId=w.id, dispatcherId=user(request),
            assigneeId=input.assigneeId, comment=input.comment,
            assignedAt=format_time(event.occurred_at), workOrderVersion=w.version)
        db.notify(session, w, event, 'M3_ASSIGNED', '工单已派单', [w.assignee_id, w.reporter_id])
        return db.mutation_output(session, w, event.id), w.id

    receipt = db.transact(request, 'dispatch_order', input, perform)
    return db.render_receipt(receipt, user(request))


def detail(session, w, *, internal=False, user_id=None):
    records = list(session.scalars(select(Record).where(Record.work_order_id == w.id)
        .order_by(Record.occurred_at, Record.version, Record.id)))
    seat = db.latest_seat(session, w)
    output = dict(workOrder=db.work_output(w), seatSync=db.seat_output(seat) if seat else None,
        assignments=[], completions=[], acceptances=[], evaluations=[], escalations=[])
    kinds = {'DISPATCH': 'assignments', 'COMPLETE': 'completions',
             'ACCEPT': 'acceptances', 'RETURN': 'acceptances', 'EVALUATE': 'evaluations'}
    for r in records:
        if r.kind in kinds and (internal or r.kind == 'EVALUATE' and r.actor_id == user_id):
            output[kinds[r.kind]].append(r.data)
        if r.kind == 'ESCALATE' and internal:
            jobs = list(session.scalars(select(Outbox).where(Outbox.record_id == r.id, Outbox.kind == 'NOTIFICATION')))
            ids = sorted({p for job in jobs for p in job.payload['recipientUserIds']})
            output['escalations'].append(r.data | {'recipientUserIds': ids,
                'notifications': [db.notification_output(j) for j in jobs]})
    timeline = [dict(eventId=r.id, type=r.kind, occurredAt=format_time(r.occurred_at),
        actorId=r.actor_id if internal or r.actor_id == user_id else None, summary=r.summary)
        for r in records if internal or r.kind != 'ESCALATE']
    return output, timeline


async def dispatch_tasks(request, input):
    scopes = await peers.permission('m3.dispatch.manage', request)
    with db.sessions()() as session:
        rows = list(session.scalars(select(WorkOrder).where(WorkOrder.status == 'WAITING_ASSIGNMENT')))
        rows = [w for w in rows if (not input.workOrderId or w.id == input.workOrderId)
            and (not input.spaceId or w.space_id == input.spaceId)
            and any(db.in_scope(s, w) for s in scopes)]
        result = paged(rows, input, request, 'dispatch-tasks', lambda w: w.id)
        result['items'] = [detail(session, w, internal=True)[0] for w in result['items']]
        return result
