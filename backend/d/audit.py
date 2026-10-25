"""D-S11：按可信来源幂等追加审计，按当前授权查询，不提供更新或删除。"""
import hashlib
import json
import logging
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import false, or_, select, text, true, tuple_
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d.audit_schemas import AUDIT_INPUT
from backend.d.models import AuditEvent, Space, User
from backend.d.pagination import decode_cursor, encode_cursor
from backend.d.permissions import check_permission_in_session, check_user_permission_in_session
from backend.shared.clock import format_time, parse_time, utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError

logger = logging.getLogger(__name__)
STORAGE_ERRORS = (InterfaceError, OperationalError, SQLAlchemyTimeoutError)


def _actor(query, mode, peer, user_id):
    if mode not in {'USER', 'SYSTEM'}:
        raise ApiError('INVALID_ARGUMENT', '调用模式必须是USER或SYSTEM')
    if peer not in {'m1', 'm2', 'm3'}:
        if not (peer is None and mode == 'USER' and query.operation in {'GET', 'LIST'}):
            raise ApiError('FORBIDDEN', '此审计操作没有可信来源资格')
    if mode == 'USER' and (not isinstance(user_id, str) or not user_id.strip()):
        raise ApiError('UNAUTHENTICATED', '需要已核验的用户身份')
    if mode == 'SYSTEM' and user_id is not None:
        raise ApiError('INVALID_ARGUMENT', '后台模式不能携带用户身份')
    if query.operation == 'APPEND':
        if query.sourceModule.lower() != peer:
            raise ApiError('FORBIDDEN', '来源与可信模块不一致')
        if mode == 'USER' and (query.actorType != 'USER' or query.actorId != user_id):
            raise ApiError('FORBIDDEN', 'USER只能记录当前已核验用户的事件')
    elif mode == 'SYSTEM' and query.sourceModule not in {None, peer.upper()}:
        raise ApiError('FORBIDDEN', '后台只能查询自己来源的审计')


def _scope(space_id):
    return {'type': 'GLOBAL', 'id': None} if space_id is None else {'type': 'SPACE', 'id': space_id}


def _access(db, code, target, mode, peer, user_id):
    if mode == 'USER':
        decision = check_user_permission_in_session(db, permission_code=code, target_scope=target, user_id=user_id)
    else:
        decision = check_permission_in_session(db, permission_code=code, target_scope=target,
                                              actor_mode=mode, source_module=peer)
    if not decision['allowed']:
        raise ApiError('FORBIDDEN', '没有该审计操作或目标范围的权限')
    return decision


def _result(row):
    actor_id = row.actor_user_id if row.actor_type == 'USER' else (
        row.source_module if row.actor_type == 'SYSTEM' else None)
    return {'auditId': row.audit_id, 'requestId': row.request_id, 'sourceModule': row.source_module,
            'actorType': row.actor_type, 'actorId': actor_id, 'objectType': row.object_type,
            'businessId': row.business_id, 'spaceId': row.space_id, 'action': row.action,
            'occurredAt': format_time(row.occurred_at), 'result': row.result,
            'summary': row.summary, 'verifiedBy': row.verified_by}


def _append(query, mode, peer, user_id):
    payload = query.model_dump(exclude={'operation', 'requestId'})
    lock_key = json.dumps(['campusflow:d:audit:v1', peer, query.requestId], ensure_ascii=False,
                          separators=(',', ':')).encode('utf-8')
    lock_id = int.from_bytes(hashlib.sha256(lock_key).digest()[:8], 'big', signed=True)
    with session_factory('d').begin() as db:
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text('SELECT pg_advisory_xact_lock(:lock_id)'), {'lock_id': lock_id})
        _access(db, 'd.audit.append', _scope(query.spaceId), mode, peer, user_id)
        previous = db.scalar(select(AuditEvent).where(
            AuditEvent.source_module == query.sourceModule, AuditEvent.request_id == query.requestId))
        if previous is not None:
            if previous.input_data != payload:
                raise ApiError('REQUEST_ID_REUSED', '审计请求编号已用于不同输入')
            result = _result(previous)
        else:
            if query.actorType == 'USER' and db.get(User, query.actorId) is None:
                raise ApiError('NOT_FOUND', '审计用户不存在')
            # 后台补报既有用户的历史事件可以引用当前已停用账号；核验责任归来源模块。
            row = AuditEvent(audit_id=f'aud-{uuid4().hex}', source_module=query.sourceModule,
                request_id=query.requestId, actor_type=query.actorType,
                actor_user_id=query.actorId if query.actorType == 'USER' else None,
                object_type=query.objectType, business_id=query.businessId, space_id=query.spaceId,
                action=query.action, occurred_at=parse_time(query.occurredAt), result=query.result,
                summary=query.summary, verified_by='D' if mode == 'USER' else query.sourceModule,
                input_data=payload, recorded_at=utc_now())
            db.add(row)
            db.flush()
            result = _result(row)
    return result


def _visible(scopes):
    if any(scope['type'] == 'GLOBAL' for scope in scopes):
        return true()
    spaces = [scope['id'] for scope in scopes if scope['type'] == 'SPACE']
    departments = [scope['id'] for scope in scopes if scope['type'] == 'DEPARTMENT']
    return or_(AuditEvent.space_id.in_(spaces), AuditEvent.space_id.in_(
        select(Space.space_id).where(Space.department_id.in_(departments)))) if spaces or departments else false()


def _query(query, mode, peer, user_id):
    with session_factory('d')() as db:
        access = _access(db, 'd.audit.read', None, mode, peer, user_id)
        scopes = access['allowedScopes']
        statement = select(AuditEvent)
        source = peer.upper() if mode == 'SYSTEM' else query.sourceModule
        if source is not None:
            statement = statement.where(AuditEvent.source_module == source)
        if query.operation == 'GET':
            statement = statement.where(AuditEvent.audit_id == query.auditId) if query.auditId is not None else (
                statement.where(AuditEvent.request_id == query.requestId))
            # 先以当前范围筛选，最多取2条即可判断可见的跨来源重名。
            rows = db.scalars(statement.where(_visible(scopes)).limit(2)).all()
            if not rows:
                exists = db.scalar(statement.with_only_columns(AuditEvent.audit_id).limit(1))
                if exists is not None:
                    raise ApiError('FORBIDDEN', '审计记录不在当前授权范围内')
                raise ApiError('NOT_FOUND', '未找到可查询审计，不代表原请求不会稍后完成')
            if len(rows) > 1:
                raise ApiError('CONFLICT', '多个可见来源使用了相同请求号，请指定sourceModule或auditId')
            return _result(rows[0])

        if query.spaceId is not None:
            _access(db, 'd.audit.read', _scope(query.spaceId), mode, peer, user_id)
        if query.actorUserId is not None and db.get(User, query.actorUserId) is None:
            raise ApiError('NOT_FOUND', '筛选用户不存在')
        context = {'service': 'd.audit', 'query': query.model_dump(exclude={'cursor'}),
                   'subjectType': access['subjectType'], 'subjectId': access['subjectId'],
                   'sourceModule': peer, 'scopes': scopes}
        after = decode_cursor(query.cursor, context)
        statement = statement.where(_visible(scopes))
        for value, column in [(query.businessId, AuditEvent.business_id), (query.objectType, AuditEvent.object_type),
                              (query.action, AuditEvent.action), (query.actorUserId, AuditEvent.actor_user_id),
                              (query.spaceId, AuditEvent.space_id)]:
            if value is not None:
                statement = statement.where(column == value)
        if query.timeFrom is not None:
            statement = statement.where(AuditEvent.occurred_at >= parse_time(query.timeFrom))
        if query.timeTo is not None:
            statement = statement.where(AuditEvent.occurred_at < parse_time(query.timeTo))
        if after is not None:
            try:
                key = json.loads(after)
                if not isinstance(key, list) or len(key) != 2 or not all(isinstance(v, str) and v for v in key):
                    raise ValueError('Bad key')
                after_time = parse_time(key[0])
            except (ValueError, TypeError):
                raise ApiError('INVALID_ARGUMENT', '审计分页游标无效') from None
            statement = statement.where(tuple_(AuditEvent.occurred_at, AuditEvent.audit_id) < (after_time, key[1]))
        rows = db.scalars(statement.order_by(AuditEvent.occurred_at.desc(), AuditEvent.audit_id.desc())
                          .limit(query.limit + 1)).all()
        selected = rows[:query.limit]
        cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            cursor = encode_cursor(json.dumps([format_time(last.occurred_at), last.audit_id]), context)
        return {'items': [_result(row) for row in selected], 'nextCursor': cursor}


def audit(*, input, actor_mode, source_module, user_id=None):
    try:
        query = AUDIT_INPUT.validate_python(input)
    except ValidationError:
        raise ApiError('INVALID_ARGUMENT', '审计字段、类型或操作不符合约定') from None
    _actor(query, actor_mode, source_module, user_id)
    try:
        return _append(query, actor_mode, source_module, user_id) if query.operation == 'APPEND' else (
            _query(query, actor_mode, source_module, user_id))
    except STORAGE_ERRORS as exc:
        logger.error('D S11 operation=%s requestId=%r reason=STORAGE_%s',
                     query.operation, getattr(query, 'requestId', None), type(exc).__name__)
        raise ApiError('UNAVAILABLE', '审计存储不可用或结果未确认，请保留原请求号核实') from None
