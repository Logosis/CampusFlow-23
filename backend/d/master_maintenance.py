"""D-S07：M1 USER维护、当前范围、永久保留座位号、版本及原结果幂等。"""
import hashlib
import logging
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d.master import _require_row, _seat, _space
from backend.d.master_maintenance_schemas import MAINTENANCE_INPUT, MAINTENANCE_RESULT
from backend.d.models import MasterRequest, Seat, SeatCodeHistory, Space
from backend.d.permissions import check_permission_in_session
from backend.d.roles import LOCK_ID as RBAC_LOCK_ID
from backend.shared.clock import utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError

logger = logging.getLogger(__name__)
LOCK_ID = int.from_bytes(hashlib.sha256(b'campusflow:d:master-maintenance:v1').digest()[:8], 'big', signed=True)


def _permission(db, user_id, target):
    access = check_permission_in_session(db, permission_code='d.master.manage', target_scope=target,
        actor_mode='USER', source_module='m1', user_id=user_id)
    if not access['allowed']:
        raise ApiError('FORBIDDEN', '没有该主数据范围的当前维护权限')


def _owner_scope(query):
    return {'type': 'GLOBAL', 'id': None} if query.ownerKind == 'SCHOOL' else {
        'type': 'DEPARTMENT', 'id': query.departmentId}


def _authorize(db, query, user_id, previous):
    if query.operation == 'CREATE_SPACE':
        _permission(db, user_id, _owner_scope(query))
        if previous is not None:
            # 原请求的创建位置不代替空间变更归属后的当前权限。
            _permission(db, user_id, {'type': 'SPACE', 'id': previous.response_data['spaceId']})
        return
    if query.operation.endswith('_SPACE') or query.operation == 'CREATE_SEAT':
        space = _require_row(db, Space, query.spaceId)
    else:
        seat = _require_row(db, Seat, query.seatId)
        space = _require_row(db, Space, seat.space_id)
    _permission(db, user_id, {'type': 'SPACE', 'id': space.space_id})
    if query.operation == 'UPDATE_SPACE' and (space.owner_kind, space.department_id) != (
            query.ownerKind, query.departmentId):
        _permission(db, user_id, _owner_scope(query))


def _registered(row):
    if not row.registered:
        raise ApiError('CONFLICT', '记录已注销，不能通过新请求修改或重新登记')


def _version(row, expected):
    if row.version != expected:
        raise ApiError('CONFLICT', '版本已变化，请重新查询后使用新请求号')


def _reserve_code(db, code):
    # 初始化或历史工具写入的当前代码也占号，即使尚无单独历史行。
    if db.get(SeatCodeHistory, code) is not None or db.scalar(select(Seat.seat_id).where(Seat.seat_code == code)) is not None:
        raise ApiError('CONFLICT', '座位号已被使用，不能重复或重新分配')
    db.add(SeatCodeHistory(seat_code=code))


def _remember_code(db, code):
    if db.get(SeatCodeHistory, code) is None:
        db.add(SeatCodeHistory(seat_code=code))


def _mutate(db, query):
    operation = query.operation
    if operation == 'CREATE_SPACE':
        row = Space(space_id=f'space-{uuid4().hex}', name=query.name, location=query.location,
            is_study_room=query.isStudyRoom, owner_kind=query.ownerKind, department_id=query.departmentId,
            registered=True, enabled=query.enabled, version=1)
        db.add(row)
    elif operation in {'UPDATE_SPACE', 'UNREGISTER_SPACE'}:
        row = _require_row(db, Space, query.spaceId)
        _version(row, query.expectedVersion)
        _registered(row)
        if operation == 'UPDATE_SPACE':
            row.name, row.location, row.is_study_room = query.name, query.location, query.isStudyRoom
            row.owner_kind, row.department_id, row.enabled = query.ownerKind, query.departmentId, query.enabled
        else:
            if db.scalar(select(Seat.seat_id).where(Seat.space_id == row.space_id, Seat.registered.is_(True)).limit(1)):
                raise ApiError('CONFLICT', '空间仍有登记座位，请先注销所有登记座位')
            row.registered, row.enabled = False, False
        row.version += 1
    elif operation == 'CREATE_SEAT':
        _registered(_require_row(db, Space, query.spaceId))
        _reserve_code(db, query.seatCode)
        row = Seat(seat_id=f'seat-{uuid4().hex}', seat_code=query.seatCode, space_id=query.spaceId,
                   registered=True, enabled=query.enabled, socket_tags=query.socketTags, version=1)
        db.add(row)
    else:
        row = _require_row(db, Seat, query.seatId)
        _version(row, query.expectedVersion)
        _registered(row)
        _registered(_require_row(db, Space, row.space_id))
        if operation == 'UPDATE_SEAT':
            if query.seatCode != row.seat_code:
                _reserve_code(db, query.seatCode)
                _remember_code(db, row.seat_code)
                row.seat_code = query.seatCode
            row.enabled, row.socket_tags = query.enabled, query.socketTags
        else:
            _remember_code(db, row.seat_code)
            row.registered, row.enabled = False, False
        row.version += 1
    db.flush()
    return _space(row) if isinstance(row, Space) else _seat(row)


def maintain_master(*, input, actor_mode, source_module, user_id=None):
    try:
        query = MAINTENANCE_INPUT.validate_python(input)
    except ValidationError:
        raise ApiError('INVALID_ARGUMENT', '主数据维护字段、类型或组合不符合约定') from None
    if source_module != 'm1' or actor_mode != 'USER':
        raise ApiError('FORBIDDEN', '主数据维护必须经过M1管理入口转发用户身份')
    if not isinstance(user_id, str) or not user_id.strip():
        raise ApiError('UNAUTHENTICATED', '需要已核验的用户身份')
    try:
        with session_factory('d').begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            # 统一主数据事务锁协调全局座位号、空间注销/新增座位及版本竞争。
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': LOCK_ID})
            # 与S05写锁配对；当前授权核验和本次提交期间不能撤销/替换用户授权。
            db.execute(text('SELECT pg_advisory_xact_lock_shared(:key)'), {'key': RBAC_LOCK_ID})
            _permission(db, user_id, None)
            payload = query.model_dump(exclude={'requestId'})
            previous = db.get(MasterRequest, query.requestId)
            if previous is not None and previous.input_data != payload:
                raise ApiError('REQUEST_ID_REUSED', '请求编号已用于不同主数据维护输入')
            _authorize(db, query, user_id, previous)
            if previous is not None:
                result = MAINTENANCE_RESULT.validate_python(previous.response_data).model_dump()
            else:
                result = _mutate(db, query)
                db.add(MasterRequest(request_id=query.requestId, input_data=payload, response_data=result,
                                     requested_by_user_id=user_id, created_at=utc_now()))
                db.flush()
        return result
    except (InterfaceError, OperationalError, SQLAlchemyTimeoutError) as exc:
        logger.error('D S07 operation=%s requestId=%r reason=STORAGE_%s', query.operation, query.requestId, type(exc).__name__)
        raise ApiError('UNAVAILABLE', '主数据存储不可用或提交结果未确认，请保留原请求号核实') from None
