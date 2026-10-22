"""D-S05：当前全局管理权限、版本及持久化幂等；不修改后台主体授权。"""
import hashlib
import logging
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d.models import Department, Permission, Role, RoleGrant, RoleRequest, Space, User, UserRole
from backend.d.pagination import decode_cursor, encode_cursor
from backend.d.permissions import check_permission_in_session
from backend.d.role_schemas import ROLE_INPUT, MUTATION_RESULT
from backend.shared.clock import utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError

logger = logging.getLogger(__name__)
WRITES = {'CREATE_ROLE', 'UPDATE_ROLE', 'DISABLE_ROLE', 'SET_USER_ROLES'}
# 管理写操作共用事务锁，避免“撤销管理员”和“继续授权”分别通过旧权限。
# 管理查询使用共享锁，确保同一响应的角色/授权来自一致的S05状态。
LOCK_ID = int.from_bytes(hashlib.sha256(b'campusflow:d:rbac:v1').digest()[:8], 'big', signed=True)


def _require(db, model, identifier):
    row = db.get(model, identifier)
    if row is None:
        raise ApiError('NOT_FOUND', '角色管理引用的记录不存在')
    return row


def _roles(db, rows):
    grants = {row.role_id: [] for row in rows}
    if grants:
        for row in db.scalars(select(RoleGrant).where(RoleGrant.role_id.in_(grants)).order_by(
                RoleGrant.permission_code.collate('C'), RoleGrant.scope_type.collate('C'),
                RoleGrant.scope_id.collate('C').nulls_first())):
            grants[row.role_id].append({'permissionCode': row.permission_code,
                'scope': {'type': row.scope_type, 'id': row.scope_id}})
    return [{'roleId': row.role_id, 'name': row.name, 'status': row.status,
             'grants': grants[row.role_id], 'version': row.version} for row in rows]


def _user_roles(db, user):
    identifiers = db.scalars(select(UserRole.role_id).where(UserRole.user_id == user.user_id)
                            .order_by(UserRole.role_id.collate('C'))).all()
    return {'userId': user.user_id, 'roleIds': identifiers, 'version': user.role_assignment_version}


def _grants(db, values):
    for grant in values:
        permission = _require(db, Permission, grant.permissionCode)
        if grant.scope.type not in permission.allowed_scope_types:
            raise ApiError('INVALID_ARGUMENT', '权限目录不允许该授权范围类型')
        if grant.scope.type == 'DEPARTMENT':
            _require(db, Department, grant.scope.id)
        elif grant.scope.type == 'SPACE':
            _require(db, Space, grant.scope.id)


def _replace_grants(db, role_id, grants):
    db.execute(delete(RoleGrant).where(RoleGrant.role_id == role_id))
    for grant in grants:
        db.add(RoleGrant(role_id=role_id, permission_code=grant.permissionCode,
                         scope_type=grant.scope.type, scope_id=grant.scope.id))


def _version(current, expected):
    if current != expected:
        raise ApiError('CONFLICT', '版本已变化，请重新查询后以新请求号修改')


def _mutate(db, query):
    if query.operation == 'CREATE_ROLE':
        _grants(db, query.grants)
        row = Role(role_id=f'role-{uuid4().hex}', name=query.name, status='ACTIVE', version=1)
        db.add(row)
        db.flush()
        _replace_grants(db, row.role_id, query.grants)
    elif query.operation in {'UPDATE_ROLE', 'DISABLE_ROLE'}:
        row = _require(db, Role, query.roleId)
        _version(row.version, query.expectedVersion)
        if query.operation == 'UPDATE_ROLE':
            _grants(db, query.grants)
            row.name, row.status = query.name, query.status
            _replace_grants(db, row.role_id, query.grants)
        else:
            row.status = 'DISABLED'
        row.version += 1
    else:
        user = _require(db, User, query.userId)
        _version(user.role_assignment_version, query.expectedVersion)
        original = set(_user_roles(db, user)['roleIds'])
        for identifier in query.roleIds:
            role = _require(db, Role, identifier)
            if role.status != 'ACTIVE' and identifier not in original:
                raise ApiError('CONFLICT', '不能新增已停用角色的用户关联')
        db.execute(delete(UserRole).where(UserRole.user_id == user.user_id))
        for identifier in query.roleIds:
            db.add(UserRole(user_id=user.user_id, role_id=identifier))
        user.role_assignment_version += 1
        db.flush()
        return _user_roles(db, user)
    db.flush()
    return _roles(db, [row])[0]


def _read(db, query, access):
    if query.operation == 'GET_ROLE':
        return _roles(db, [_require(db, Role, query.roleId)])[0]
    if query.operation == 'GET_USER_ROLES':
        return _user_roles(db, _require(db, User, query.userId))
    context = {'service': 'd.manage_roles', 'query': query.model_dump(exclude={'cursor'}),
               'subjectId': access['subjectId'], 'sourceModule': 'm1', 'scopes': access['allowedScopes'],
               'ordering': 'identifier-C-v1'}
    after = decode_cursor(query.cursor, context)
    if query.operation == 'LIST_PERMISSIONS':
        statement, key = select(Permission), Permission.code
    elif query.operation == 'LIST_USERS':
        statement, key = select(User), User.user_id
        if query.departmentId is not None:
            _require(db, Department, query.departmentId)
            statement = statement.where(User.department_id == query.departmentId)
        if query.accountStatus is not None:
            statement = statement.where(User.account_status == query.accountStatus)
    else:
        statement, key = select(Role), Role.role_id
        if query.status is not None:
            statement = statement.where(Role.status == query.status)
    # UTF-8下C排序与Python集合规范化一致；续查比较和ORDER BY必须使用同一规则。
    key = key.collate('C')
    if after is not None:
        statement = statement.where(key > after)
    rows = db.scalars(statement.order_by(key).limit(query.limit + 1)).all()
    selected = rows[:query.limit]
    if query.operation == 'LIST_PERMISSIONS':
        items = [{'code': r.code, 'name': r.name, 'allowedScopeTypes': sorted(set(r.allowed_scope_types))}
                 for r in selected]
        last = selected[-1].code if selected else None
    elif query.operation == 'LIST_USERS':
        items = [{'userId': r.user_id, 'displayName': r.display_name, 'departmentId': r.department_id,
                  'accountStatus': r.account_status} for r in selected]
        last = selected[-1].user_id if selected else None
    else:
        items = _roles(db, selected)
        last = selected[-1].role_id if selected else None
    return {'items': items, 'nextCursor': encode_cursor(last, context) if len(rows) > query.limit else None}


def manage_roles(*, input, actor_mode, source_module, user_id=None):
    try:
        query = ROLE_INPUT.validate_python(input)
    except ValidationError:
        raise ApiError('INVALID_ARGUMENT', '角色管理字段、类型或集合不符合约定') from None
    if source_module != 'm1' or actor_mode != 'USER':
        raise ApiError('FORBIDDEN', '角色管理必须由M1管理入口传递用户身份')
    if not isinstance(user_id, str) or not user_id.strip():
        raise ApiError('UNAUTHENTICATED', '需要已核验的用户身份')
    writing = query.operation in WRITES
    try:
        with session_factory('d').begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            lock = 'pg_advisory_xact_lock' if writing else 'pg_advisory_xact_lock_shared'
            db.execute(text(f'SELECT {lock}(:key)'), {'key': LOCK_ID})
            access = check_permission_in_session(db, permission_code='d.rbac.manage' if writing else 'd.rbac.read',
                target_scope={'type': 'GLOBAL', 'id': None}, actor_mode='USER', source_module='m1', user_id=user_id)
            if not access['allowed']:
                raise ApiError('FORBIDDEN', '角色管理需要当前全局查询或管理权限')
            if not writing:
                result = _read(db, query, access)
            else:
                payload = query.model_dump(exclude={'requestId'})
                previous = db.get(RoleRequest, query.requestId)
                if previous is not None:
                    if previous.input_data != payload:
                        raise ApiError('REQUEST_ID_REUSED', '请求编号已用于不同的角色管理输入')
                    result = MUTATION_RESULT.validate_python(previous.response_data).model_dump()
                else:
                    result = _mutate(db, query)
                    db.add(RoleRequest(request_id=query.requestId, input_data=payload, response_data=result,
                                       requested_by_user_id=user_id, created_at=utc_now()))
                    db.flush()
        return result
    except (InterfaceError, OperationalError, SQLAlchemyTimeoutError) as exc:
        logger.error('D S05 operation=%s requestId=%r reason=STORAGE_%s', query.operation,
                     getattr(query, 'requestId', None), type(exc).__name__)
        raise ApiError('UNAVAILABLE', '角色管理存储不可用或提交结果未确认，请保留原请求号核实') from None
