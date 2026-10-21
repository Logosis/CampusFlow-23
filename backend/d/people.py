"""D-S08：按目标业务范围核验当前人员资格，只读取约定的基本资料。"""

from sqlalchemy import and_, false, or_, select
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d.models import Permission, Role, RoleGrant, User, UserRole
from backend.d.pagination import decode_cursor, encode_cursor
from backend.d.people_schemas import PEOPLE_INPUT
from backend.d.permissions import _target_department, check_permission_in_session
from backend.shared.db import session_factory
from backend.shared.errors import ApiError


def _qualification(permission, target, target_department):
    """相关 EXISTS：任一有效角色可提供一项资格，多项资格可来自不同角色。"""
    scopes = []
    allowed_types = permission.allowed_scope_types
    if "GLOBAL" in allowed_types:
        scopes.append(and_(RoleGrant.scope_type == "GLOBAL", RoleGrant.scope_id.is_(None)))
    if "DEPARTMENT" in allowed_types and target["type"] != "GLOBAL" and target_department is not None:
        scopes.append(and_(RoleGrant.scope_type == "DEPARTMENT", RoleGrant.scope_id == target_department))
    if "SPACE" in allowed_types and target["type"] == "SPACE":
        scopes.append(and_(RoleGrant.scope_type == "SPACE", RoleGrant.scope_id == target["id"]))
    if not scopes:
        return false()
    return (
        select(1).select_from(UserRole)
        .join(Role, Role.role_id == UserRole.role_id)
        .join(RoleGrant, RoleGrant.role_id == Role.role_id)
        .where(
            UserRole.user_id == User.user_id, Role.status == "ACTIVE",
            RoleGrant.permission_code == permission.code, or_(*scopes),
        )
        .correlate(User).exists()
    )


def _people_statement(permissions, target, target_department):
    matches = [_qualification(permission, target, target_department) for permission in permissions]
    statement = select(
        User.user_id, User.display_name, User.department_id, User.account_status,
        *(condition.label(f"matches_{index}") for index, condition in enumerate(matches)),
    )
    return statement, matches


def _person(row, codes, display):
    matched = []
    if not display and row.account_status == "ACTIVE":
        matched = [code for index, code in enumerate(codes) if row._mapping[f"matches_{index}"]]
    return {
        "userId": row.user_id, "displayName": row.display_name,
        "departmentId": row.department_id, "accountStatus": row.account_status,
        "eligible": None if display else row.account_status == "ACTIVE" and len(matched) == len(codes),
        "matchedPermissionCodes": matched,
    }


def query_people(*, input, actor_mode, source_module, user_id=None):
    query = PEOPLE_INPUT.validate_python(input)
    if source_module not in {"m2", "m3"}:
        raise ApiError("FORBIDDEN", "此人员服务仅供M2和M3调用")
    try:
        with session_factory("d")() as db:
            target = query.targetScope.model_dump()
            access = check_permission_in_session(
                db, permission_code="d.people.read", target_scope=target,
                actor_mode=actor_mode, source_module=source_module, user_id=user_id,
            )
            if not access["allowed"]:
                raise ApiError("FORBIDDEN", "没有该范围的人员查询权限")
            codes = sorted(set(query.permissionCodes))
            catalog = db.scalars(select(Permission).where(Permission.code.in_(codes))).all() if codes else []
            if len(catalog) != len(codes):
                raise ApiError("NOT_FOUND", "资格权限编码不存在")
            by_code = {permission.code: permission for permission in catalog}
            permissions = [by_code[code] for code in codes]
            target_department = _target_department(db, target)
            statement, matches = _people_statement(permissions, target, target_department)
            if query.mode == "BY_IDS":
                if not query.userIds:
                    return {"people": []}
                rows = db.execute(statement.where(User.user_id.in_(set(query.userIds)))).all()
                by_id = {row.user_id: _person(row, codes, query.purpose == "DISPLAY") for row in rows}
                if set(query.userIds) - by_id.keys():
                    raise ApiError("NOT_FOUND", "请求的人员中包含不存在的用户")
                return {"people": [by_id[identifier] for identifier in query.userIds]}

            context = {
                "service": "d.query_people", "query": {**query.model_dump(exclude={"cursor"}), "permissionCodes": codes},
                "subjectType": access["subjectType"], "subjectId": access["subjectId"],
                "sourceModule": source_module, "scopes": access["allowedScopes"],
            }
            after = decode_cursor(query.cursor, context)
            statement = statement.where(User.account_status == "ACTIVE", *matches)
            if after is not None:
                statement = statement.where(User.user_id > after)
            rows = db.execute(statement.order_by(User.user_id).limit(query.limit + 1)).all()
            items = [_person(row, codes, False) for row in rows[:query.limit]]
            next_cursor = encode_cursor(rows[query.limit - 1].user_id, context) if len(rows) > query.limit else None
            return {"items": items, "nextCursor": next_cursor}
    except (OperationalError, SQLAlchemyTimeoutError):
        raise ApiError("UNAVAILABLE", "人员数据暂时不可用，请稍后重试") from None
