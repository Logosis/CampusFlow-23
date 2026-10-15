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
    previous = db.existing_receipt(request, 'report_fault', input)
    if previous:
        return db.render_receipt(previous, user(request))
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
    from backend.m3.worker import process_jobs
    await process_jobs(db.render_receipt(receipt)['workOrder']['workOrderId'])
    return db.render_receipt(receipt, user(request))


async def candidates(request, input):
    w = await authorize_order(request, input.workOrderId, 'm3.dispatch.manage')
    if w.status != 'WAITING_ASSIGNMENT':
        raise ApiError('CONFLICT', '当前工单不在待派单阶段')
    people = await peers.people(w.space_id, 'DISPATCH', ['m3.repair.manage'], request)
    return paged(people, input, request, 'dispatch-candidates', lambda p: p['userId'])


async def dispatch(request, input):
    w = await authorize_order(request, input.workOrderId, 'm3.dispatch.manage')
    previous = db.existing_receipt(request, 'dispatch_order', input)
    if previous:
        return db.render_receipt(previous, user(request))
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
    from backend.m3.worker import process_jobs
    await process_jobs(w.id)
    return db.render_receipt(receipt, user(request))


def detail(session, w, *, internal=False, user_id=None, management=False):
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
        if r.kind == 'ESCALATE' and management:
            jobs = list(session.scalars(select(Outbox).where(Outbox.record_id == r.id, Outbox.kind == 'NOTIFICATION')))
            ids = sorted({p for job in jobs for p in job.payload['recipientUserIds']})
            output['escalations'].append(r.data | {'recipientUserIds': ids,
                'notifications': [db.notification_output(j) for j in jobs]})
    timeline = [dict(eventId=r.id, type=r.kind, occurredAt=format_time(r.occurred_at),
        actorId=r.actor_id if internal or r.actor_id == user_id else None, summary=r.summary)
        for r in records if management or r.kind != 'ESCALATE']
    return output, timeline


async def dispatch_tasks(request, input):
    with db.sessions()() as session:
        rows = list(session.scalars(select(WorkOrder).where(WorkOrder.status == 'WAITING_ASSIGNMENT')))
    allowed = await allowed_space_ids(request, ['m3.dispatch.manage'], [w.space_id for w in rows])
    with db.sessions()() as session:
        rows = [w for w in rows if (not input.workOrderId or w.id == input.workOrderId)
            and (not input.spaceId or w.space_id == input.spaceId)
            and w.space_id in allowed]
        result = paged(rows, input, request, 'dispatch-tasks', lambda w: w.id)
        result['items'] = [detail(session, w, internal=True, management=True)[0] for w in result['items']]
        return result


async def mutate(request, input, function):
    codes = {'start_repair': 'm3.repair.manage', 'submit_completion': 'm3.repair.manage',
        'submit_acceptance': 'm3.acceptance.manage', 'submit_evaluation': 'm3.evaluation.create',
        'cancel_work_order': 'm3.work_order.cancel'}
    snapshot = await authorize_order(request, input.workOrderId, codes[function])
    actor = user(request)

    def check_relation(w):
        if function in ('start_repair', 'submit_completion') and w.assignee_id != actor:
            raise ApiError('FORBIDDEN', '只允许当前承接人处理')
        if function == 'submit_acceptance' and w.assignee_id == actor:
            raise ApiError('FORBIDDEN', '承接人不能验收自己的工单')
        if function in ('submit_evaluation', 'cancel_work_order') and w.reporter_id != actor:
            raise ApiError('FORBIDDEN', '只允许本单上报人操作')
    check_relation(snapshot)

    def perform(session):
        w = db.get_order(session, input.workOrderId, locked=True)
        check_relation(w)
        now = utc_now()
        if function == 'submit_evaluation':
            if w.status != 'ACCEPTED':
                raise ApiError('CONFLICT', '验收通过后才能评价')
            if session.scalar(select(Record.id).where(Record.work_order_id == w.id, Record.kind == 'EVALUATE')):
                raise ApiError('CONFLICT', '每个工单只能评价一次')
            w.version += 1
            w.updated_at = now
            event = db.record_event(session, w, 'EVALUATE', actor, '上报人已评价')
            event.data = dict(evaluationId=event.id, workOrderId=w.id, evaluatorId=actor,
                content=input.content, createdAt=format_time(now))
        else:
            stages = {'start_repair': ('ASSIGNED',), 'submit_completion': ('REPAIRING',),
                'submit_acceptance': ('WAITING_ACCEPTANCE',),
                'cancel_work_order': ('WAITING_ASSIGNMENT', 'ASSIGNED')}
            db.expect(w, input.expectedVersion, stages[function])
            if function == 'cancel_work_order' and (w.first_repair_started_at is not None
                    or session.scalar(select(Outbox.id).where(Outbox.work_order_id == w.id, Outbox.kind == 'SEAT'))):
                raise ApiError('CONFLICT', '工单已经开始维修或产生下架请求，不能取消')
            if function == 'submit_acceptance':
                completion = session.scalar(select(Record).where(Record.work_order_id == w.id,
                    Record.kind == 'COMPLETE').order_by(Record.version.desc()).limit(1))
                if completion is None or completion.id != input.completionId:
                    raise ApiError('CONFLICT', '必须验收当前轮次的完工记录')
            w.version += 1
            w.updated_at = now
            if function == 'start_repair':
                w.first_repair_started_at = now
                db.enter_stage(session, w, 'REPAIRING', now)
                event = db.record_event(session, w, 'REPAIR', actor, '维修已开始')
                db.seat_job(session, w, 'REPAIR')
                db.notify(session, w, event, 'M3_REPAIR_STARTED', '维修已开始', [w.reporter_id])
            elif function == 'submit_completion':
                db.enter_stage(session, w, 'WAITING_ACCEPTANCE', now)
                event = db.record_event(session, w, 'COMPLETE', actor, '维修人员已提交完工')
                event.data = dict(completionId=event.id, workOrderId=w.id, submittedBy=actor,
                    summary=input.summary, submittedAt=format_time(now), workOrderVersion=w.version)
                db.notify(session, w, event, 'M3_ACCEPTANCE_PENDING', '工单等待验收',
                    selector=dict(purpose='ACCEPTANCE', permissionCodes=['m3.acceptance.manage'],
                                  excludeUserIds=[w.assignee_id]))
            elif function == 'submit_acceptance':
                passed = input.decision == 'PASS'
                db.enter_stage(session, w, 'ACCEPTED' if passed else 'REPAIRING', now)
                if passed:
                    w.accepted_at = now
                    db.seat_job(session, w, 'RELEASE')
                event = db.record_event(session, w, 'ACCEPT' if passed else 'RETURN', actor,
                    '验收通过' if passed else '验收退回返修')
                event.data = dict(acceptanceId=event.id, workOrderId=w.id,
                    completionId=input.completionId, acceptedBy=actor, decision=input.decision,
                    comment=input.comment, decidedAt=format_time(now), workOrderVersion=w.version)
                db.notify(session, w, event, 'M3_ACCEPTANCE_RESULT', event.summary, [w.reporter_id, w.assignee_id])
            else:
                db.enter_stage(session, w, 'CANCELLED', now)
                event = db.record_event(session, w, 'CANCEL', actor, '上报人已取消工单', {'reason': input.reason})
                db.notify(session, w, event, 'M3_CANCELLED', '工单已取消',
                    [p for p in (w.reporter_id, w.assignee_id) if p])
        return db.mutation_output(session, w, event.id), w.id

    receipt = db.transact(request, function, input, perform)
    from backend.m3.worker import process_jobs
    await process_jobs(snapshot.id)
    return db.render_receipt(receipt, actor)


async def work_tasks(request, input):
    codes = {'REPAIR': 'm3.repair.manage', 'ACCEPTANCE': 'm3.acceptance.manage', 'EVALUATION': 'm3.evaluation.create'}
    actor = user(request)
    with db.sessions()() as session:
        rows = list(session.scalars(select(WorkOrder)))
    allowed = await allowed_space_ids(request, [codes[input.purpose]], [w.space_id for w in rows])
    with db.sessions()() as session:
        def visible(w):
            if input.workOrderId and w.id != input.workOrderId or input.spaceId and w.space_id != input.spaceId:
                return False
            if w.space_id not in allowed:
                return False
            if input.purpose == 'REPAIR':
                return w.assignee_id == actor and (bool(input.workOrderId) or w.status in ('ASSIGNED', 'REPAIRING'))
            if input.purpose == 'ACCEPTANCE':
                return w.assignee_id != actor and w.assignee_id is not None and (bool(input.workOrderId) or w.status == 'WAITING_ACCEPTANCE')
            if w.reporter_id != actor or w.status != 'ACCEPTED':
                return False
            evaluated = session.scalar(select(Record.id).where(Record.work_order_id == w.id, Record.kind == 'EVALUATE'))
            return bool(input.workOrderId) or not evaluated
        rows = [w for w in rows if visible(w)]
        result = paged(rows, input, request, 'work-tasks', lambda w: w.id)
        result['items'] = [detail(session, w, internal=input.purpose != 'EVALUATION', user_id=actor)[0] for w in result['items']]
        return result


async def allowed_space_ids(request, codes, space_ids):
    # Re-check each current SPACE scope through D; stored department IDs are timer snapshots,
    # not an authorization source after a master-data change.
    for code in codes:
        await peers.permission(code, request)
    allowed = set()
    for ident in sorted(set(space_ids)):
        if all([bool(await peers.permission(code, request, ident, required=False)) for code in codes]):
            allowed.add(ident)
    return allowed


async def orders(request, input):
    codes = ['m3.work_order.read'] + (['m3.work_order.read_all'] if input.view == 'MANAGED' else [])
    with db.sessions()() as session:
        stmt = select(WorkOrder)
        if input.view == 'OWN':
            stmt = stmt.where(WorkOrder.reporter_id == user(request))
        elif input.view == 'ASSIGNED':
            stmt = stmt.where(WorkOrder.assignee_id == user(request))
        if input.status:
            stmt = stmt.where(WorkOrder.status == input.status)
        if input.spaceId:
            stmt = stmt.where(WorkOrder.space_id == input.spaceId)
        if input.startTime:
            from backend.shared.clock import parse_time
            stmt = stmt.where(WorkOrder.created_at >= parse_time(input.startTime), WorkOrder.created_at < parse_time(input.endTime))
        rows = list(session.scalars(stmt))
    allowed = await allowed_space_ids(request, codes, [w.space_id for w in rows])
    return paged([db.work_output(w) for w in rows if w.space_id in allowed], input, request,
        'orders', lambda w: w['workOrderId'])


async def order_detail(request, input):
    w = await authorize_order(request, input.workOrderId, 'm3.work_order.read')
    own = user(request) in (w.reporter_id, w.assignee_id)
    managed = await peers.permission('m3.work_order.read_all', request, w.space_id, required=False)
    if not own and not managed:
        raise ApiError('FORBIDDEN', '无权查看此工单')
    # A reporter with read permission sees public progress; operational records require
    # repair/dispatch/acceptance permission or read_all, independently of ownership.
    operational = bool(managed)
    if not operational and w.assignee_id == user(request):
        operational = bool(await peers.permission('m3.repair.manage', request, w.space_id, required=False))
    with db.sessions()() as session:
        output, timeline = detail(session, db.get_order(session, w.id), internal=operational, user_id=user(request), management=bool(managed))
    return {'detail': output, 'timeline': timeline}


async def stats(request, input):
    from backend.shared.clock import parse_time
    start, end = parse_time(input.startTime), parse_time(input.endTime)
    with db.sessions()() as session:
        stmt = select(WorkOrder).where(WorkOrder.created_at >= start, WorkOrder.created_at < end)
        if input.spaceId:
            stmt = stmt.where(WorkOrder.space_id == input.spaceId)
        rows = list(session.scalars(stmt))
    allowed = await allowed_space_ids(request, ['m3.statistics.read'], [w.space_id for w in rows])
    if input.spaceId:
        await peers.permission('m3.statistics.read', request, input.spaceId)
    rows = [w for w in rows if w.space_id in allowed]
    counts = {key: sum(w.status == key for w in rows) for key in db.STATUSES}
    samples = [(w.accepted_at - w.first_repair_started_at).total_seconds() for w in rows
        if w.status == 'ACCEPTED' and w.first_repair_started_at and w.accepted_at and w.accepted_at >= w.first_repair_started_at]
    with db.sessions()() as session:
        overdue = set(session.scalars(select(Record.work_order_id).where(Record.kind == 'ESCALATE',
            Record.work_order_id.in_([w.id for w in rows]))))
    return dict(startTime=format_time(start), endTime=format_time(end), spaceId=input.spaceId,
        checkedAt=format_time(utc_now()), totalCount=len(rows), statusCounts=counts,
        overdueCount=len(overdue), averageRepairSeconds=sum(samples) / len(samples) if samples else None,
        sampleCount=len(samples))


async def get_config(request):
    await peers.permission('m3.config.read', request, global_scope=True)
    with db.sessions()() as session:
        return db.config(session)


async def update_config(request, input):
    from backend.m3.models import TimeoutConfig
    await peers.permission('m3.config.manage', request, global_scope=True)
    previous = db.existing_receipt(request, 'update_timeout_config', input)
    if previous:
        return db.render_receipt(previous)
    rules = [r.model_dump() for r in input.rules]
    if len({r['ruleId'] for r in rules}) != len(rules):
        raise ApiError('INVALID_ARGUMENT', '规则编号不能重复')
    spaces = {}
    for rule in rules:
        scope = rule['scope']
        if scope['type'] == 'SPACE':
            spaces[scope['id']] = await peers.get_space(scope['id'], request)
        elif scope['type'] == 'DEPARTMENT':
            from backend.m3.schemas import Department
            departments = await peers.collect('d', 'query_master', {'operation': 'LIST_DEPARTMENTS'}, Department, request)
            if scope['id'] not in {d['departmentId'] for d in departments}:
                raise ApiError('INVALID_ARGUMENT', '规则引用不存在的院系')
        # D rejects unknown permission codes; the result may legitimately contain no users.
        await peers.collect('d', 'query_people', dict(mode='CANDIDATES', purpose='ESCALATION',
            permissionCodes=rule['recipientPermissionCodes'], targetScope=scope), peers.Person, request)
    for i, left in enumerate(rules):
        for right in rules[i + 1:]:
            if left['stage'] != right['stage']:
                continue
            a, b = left['scope'], right['scope']
            overlap = a == b or a['type'] == 'GLOBAL' or b['type'] == 'GLOBAL'
            if a['type'] == 'DEPARTMENT' and b['type'] == 'SPACE':
                overlap = spaces[b['id']]['departmentId'] == a['id']
            if b['type'] == 'DEPARTMENT' and a['type'] == 'SPACE':
                overlap = spaces[a['id']]['departmentId'] == b['id']
            if overlap:
                raise ApiError('INVALID_ARGUMENT', '同阶段的规则范围不能重叠')

    def perform(session):
        db.lock_key(session, 'm3:config')
        cfg = db.config(session)
        if cfg['version'] != input.expectedVersion:
            raise ApiError('CONFLICT', '配置版本已变化')
        data = dict(version=cfg['version'] + 1, checkIntervalSeconds=input.checkIntervalSeconds, rules=rules)
        row = session.get(TimeoutConfig, 1)
        if row:
            row.data = data
        else:
            session.add(TimeoutConfig(id=1, data=data))
        db.add_job(session, None, 'AUDIT', dict(operation='APPEND', sourceModule='M3', actorType='USER',
            actorId=user(request), objectType='TIMEOUT_CONFIG', businessId='m3-timeout-config', spaceId=None,
            action='UPDATE_TIMEOUT_CONFIG', occurredAt=format_time(utc_now()), result='SUCCESS', summary='更新维修超时配置'))
        return data, None
    receipt = db.transact(request, 'update_timeout_config', input, perform)
    return db.render_receipt(receipt)
