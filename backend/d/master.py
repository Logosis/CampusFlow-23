"""D-S06：主数据只读查询，数据库内筛选授权范围并按稳定标识分页。"""

from sqlalchemy import cast, false, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d.master_schemas import MASTER_INPUT
from backend.d.models import Department, Seat, Space
from backend.d.pagination import decode_cursor, encode_cursor
from backend.d.permissions import check_permission_in_session
from backend.shared.db import session_factory
from backend.shared.errors import ApiError


def _scope_filter(scopes, departments=False):
    if any(scope["type"] == "GLOBAL" for scope in scopes):
        return true()
    dep_ids = [scope["id"] for scope in scopes if scope["type"] == "DEPARTMENT"]
    if departments:
        # SPACE 不向上扩大成院系访问权，与 S04 覆盖规则一致。
        return Department.department_id.in_(dep_ids) if dep_ids else false()
    space_ids = [scope["id"] for scope in scopes if scope["type"] == "SPACE"]
    return or_(Space.department_id.in_(dep_ids), Space.space_id.in_(space_ids))


def _department(row):
    return {"departmentId": row.department_id, "name": row.name}


def _space(row):
    return {
        "spaceId": row.space_id, "name": row.name, "location": row.location,
        "isStudyRoom": row.is_study_room, "ownerKind": row.owner_kind,
        "departmentId": row.department_id, "registered": row.registered,
        "enabled": row.enabled, "version": row.version,
    }


def _seat(row):
    return {
        "seatId": row.seat_id, "seatCode": row.seat_code, "spaceId": row.space_id,
        "registered": row.registered, "enabled": row.enabled,
        "socketTags": sorted(row.socket_tags), "version": row.version,
    }


def _require_row(db, model, identifier):
    row = db.get(model, identifier)
    if row is None:
        raise ApiError("NOT_FOUND", "指定的主数据记录不存在")
    return row


def _detail(db, query, scopes):
    if query.operation == "GET_SPACE":
        row = _require_row(db, Space, query.spaceId)
        space = row
        serialize = _space
    else:
        row = _require_row(db, Seat, query.seatId)
        space = _require_row(db, Space, row.space_id)
        serialize = _seat
    if not query.includeUnregistered and (not row.registered or not space.registered):
        raise ApiError("NOT_FOUND", "记录已注销，请显式查询包含注销记录")
    allowed = db.scalar(select(Space.space_id).where(
        Space.space_id == space.space_id, _scope_filter(scopes),
    ))
    if allowed is None:
        raise ApiError("FORBIDDEN", "无权读取该主数据范围")
    return serialize(row)


def _list(db, query, access, source_module):
    scopes = access["allowedScopes"]
    context = {
        "service": "d.query_master", "query": query.model_dump(exclude={"cursor"}),
        "subjectType": access["subjectType"], "subjectId": access["subjectId"],
        "sourceModule": source_module, "scopes": scopes,
    }
    after = decode_cursor(query.cursor, context)
    if query.operation == "LIST_DEPARTMENTS":
        key, serialize = Department.department_id, _department
        stmt = select(Department).where(_scope_filter(scopes, departments=True))
    else:
        if query.departmentId is not None:
            _require_row(db, Department, query.departmentId)
        if query.operation == "LIST_SPACES":
            key, serialize = Space.space_id, _space
            stmt = select(Space)
            if query.ownerKind is not None:
                stmt = stmt.where(Space.owner_kind == query.ownerKind)
            if query.isStudyRoom is not None:
                stmt = stmt.where(Space.is_study_room == query.isStudyRoom)
            if query.enabled is not None:
                stmt = stmt.where(Space.enabled == query.enabled)
        else:
            key, serialize = Seat.seat_id, _seat
            stmt = select(Seat).join(Space, Space.space_id == Seat.space_id)
            if query.spaceId is not None:
                _require_row(db, Space, query.spaceId)
                stmt = stmt.where(Seat.space_id == query.spaceId)
            if query.enabled is not None:
                stmt = stmt.where(Seat.enabled == query.enabled)
            if query.socketTag is not None:
                stmt = stmt.where(cast(Seat.socket_tags, JSONB).contains([query.socketTag]))
            if not query.includeUnregistered:
                stmt = stmt.where(Seat.registered.is_(True))
        stmt = stmt.where(_scope_filter(scopes))
        if query.departmentId is not None:
            stmt = stmt.where(Space.department_id == query.departmentId)
        if not query.includeUnregistered:
            stmt = stmt.where(Space.registered.is_(True))
    if after is not None:
        stmt = stmt.where(key > after)
    rows = db.scalars(stmt.order_by(key).limit(query.limit + 1)).all()
    items = [serialize(row) for row in rows[:query.limit]]
    next_cursor = None
    if len(rows) > query.limit:
        next_cursor = encode_cursor(getattr(rows[query.limit - 1], key.key), context)
    return {"items": items, "nextCursor": next_cursor}


def query_master(*, input, actor_mode, source_module, user_id=None):
    query = MASTER_INPUT.validate_python(input)
    try:
        with session_factory("d")() as db:
            access = check_permission_in_session(
                db, permission_code="d.master.read", target_scope=None,
                actor_mode=actor_mode, source_module=source_module, user_id=user_id,
            )
            if not access["allowed"]:
                raise ApiError("FORBIDDEN", "没有主数据读取权限")
            if query.operation in {"GET_SPACE", "GET_SEAT"}:
                return _detail(db, query, access["allowedScopes"])
            return _list(db, query, access, source_module)
    except (OperationalError, SQLAlchemyTimeoutError):
        raise ApiError("UNAVAILABLE", "主数据存储暂时不可用，请稍后重试") from None
