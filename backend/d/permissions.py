"""D-S04：只读权限判断，主体必须来自共同 guard 核验后的请求状态。"""

from sqlalchemy import and_, select
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d.identity import build_identity
from backend.d.models import Department, Permission, Space, SystemGrant, SystemSubject
from backend.shared.db import session_factory
from backend.shared.errors import ApiError


def _target_department(db, target_scope):
    """校验目标引用；返回 SPACE 的院系归属，学校空间返回 None。"""
    if target_scope is None or target_scope["type"] == "GLOBAL":
        return None
    if target_scope["type"] == "DEPARTMENT":
        if db.get(Department, target_scope["id"]) is None:
            raise ApiError("NOT_FOUND", "目标院系不存在")
        return target_scope["id"]
    space = db.get(Space, target_scope["id"])
    if space is None:
        raise ApiError("NOT_FOUND", "目标空间不存在")
    # 停用/注销不抹除历史权限范围；是否允许具体业务由业务服务检查。
    return space.department_id


def _effective_scopes(db, permission, grants):
    scopes = set()
    for grant in grants:
        if grant["permissionCode"] != permission.code:
            continue
        scope = grant["scope"]
        kind, identifier = scope["type"], scope["id"]
        if kind not in permission.allowed_scope_types:
            continue
        if kind == "GLOBAL" and identifier is None:
            scopes.add((kind, identifier))
        elif kind == "DEPARTMENT" and identifier and db.get(Department, identifier) is not None:
            scopes.add((kind, identifier))
        elif kind == "SPACE" and identifier and db.get(Space, identifier) is not None:
            scopes.add((kind, identifier))
    return [
        {"type": kind, "id": identifier}
        for kind, identifier in sorted(scopes, key=lambda item: (item[0], item[1] or ""))
    ]


def _covers(scope, target, target_department):
    if scope["type"] == "GLOBAL":
        return True
    if target["type"] == "GLOBAL":
        return False
    if scope == target:
        return True
    return (
        scope["type"] == "DEPARTMENT"
        and target["type"] == "SPACE"
        and target_department is not None
        and scope["id"] == target_department
    )


def check_permission_in_session(db, *, permission_code, target_scope, actor_mode, source_module, user_id=None):
    """供本模块查询复用同一数据库会话；规则与 S04 一致。"""
    if source_module not in {"m1", "m2", "m3"}:
        raise ApiError("FORBIDDEN", "调用模块没有此服务资格")
    if actor_mode not in {"USER", "SYSTEM"}:
        raise ApiError("INVALID_ARGUMENT", "模块调用必须指定USER或SYSTEM")
    if actor_mode == "USER" and (not isinstance(user_id, str) or not user_id.strip()):
        raise ApiError("UNAUTHENTICATED", "需要已核验的用户身份")
    if actor_mode == "SYSTEM" and user_id is not None:
        raise ApiError("INVALID_ARGUMENT", "后台模式不能携带用户身份")

    permission = db.get(Permission, permission_code)
    if permission is None:
        raise ApiError("NOT_FOUND", "权限编码不存在")
    target_department = _target_department(db, target_scope)
    subject_id = user_id if actor_mode == "USER" else source_module
    subject_reason = None
    if actor_mode == "USER":
        grants = build_identity(db, user_id)["grants"]
    else:
        rows = db.execute(
            select(
                SystemSubject.status, SystemGrant.permission_code,
                SystemGrant.scope_type, SystemGrant.scope_id,
            )
            .select_from(SystemSubject)
            .outerjoin(SystemGrant, and_(
                SystemGrant.module_id == SystemSubject.module_id,
                SystemGrant.permission_code == permission_code,
            ))
            .where(SystemSubject.module_id == source_module)
        ).all()
        if not rows:
            subject_reason = "SUBJECT_UNREGISTERED"
        elif rows[0].status != "ACTIVE":
            subject_reason = "SUBJECT_DISABLED"
        grants = [] if subject_reason else [
            {"permissionCode": row.permission_code,
             "scope": {"type": row.scope_type, "id": row.scope_id}}
            for row in rows if row.permission_code is not None
        ]
    scopes = _effective_scopes(db, permission, grants)
    allowed = bool(scopes) if target_scope is None else any(
        _covers(scope, target_scope, target_department) for scope in scopes
    )
    reason = subject_reason or (
        "GRANTED" if allowed else ("OUT_OF_SCOPE" if scopes else "NO_GRANT")
    )
    return {
        "subjectType": actor_mode,
        "subjectId": subject_id,
        "allowed": allowed,
        "allowedScopes": scopes,
        "reason": reason,
    }


def check_permission(*, permission_code, target_scope, actor_mode, source_module, user_id=None):
    """USER 重新汇总有效角色；SYSTEM 按可信来源模块读取独立授权。"""
    if source_module not in {"m1", "m2", "m3"}:
        raise ApiError("FORBIDDEN", "调用模块没有此服务资格")
    if actor_mode not in {"USER", "SYSTEM"}:
        raise ApiError("INVALID_ARGUMENT", "模块调用必须指定USER或SYSTEM")
    if actor_mode == "USER" and (not isinstance(user_id, str) or not user_id.strip()):
        raise ApiError("UNAUTHENTICATED", "需要已核验的用户身份")
    if actor_mode == "SYSTEM" and user_id is not None:
        raise ApiError("INVALID_ARGUMENT", "后台模式不能携带用户身份")

    try:
        with session_factory("d")() as db:
            return check_permission_in_session(
                db, permission_code=permission_code, target_scope=target_scope,
                actor_mode=actor_mode, source_module=source_module, user_id=user_id,
            )
    except (OperationalError, SQLAlchemyTimeoutError):
        raise ApiError("UNAVAILABLE", "权限存储暂时不可用，请稍后重试") from None
