"""供登录与凭据校验复用的身份汇总，不是公开的按用户编号查询接口。"""

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from backend.d.models import Role, RoleGrant, User, UserRole
from backend.shared.errors import ApiError


def build_identity(db: Session, user_id: str) -> dict:
    """调用方先核验身份；这里只返回当前有效角色及其去重后的授权。"""
    # 一次查询读取用户、有效角色与授权，避免多次查询之间出现不同快照。
    # 外连接保留没有角色或没有授权的有效用户。
    rows = db.execute(
        select(
            User.user_id,
            User.display_name,
            User.department_id,
            User.account_status,
            Role.role_id,
            RoleGrant.permission_code,
            RoleGrant.scope_type,
            RoleGrant.scope_id,
        )
        .select_from(User)
        .outerjoin(UserRole, UserRole.user_id == User.user_id)
        .outerjoin(
            Role,
            and_(Role.role_id == UserRole.role_id, Role.status == "ACTIVE"),
        )
        .outerjoin(RoleGrant, RoleGrant.role_id == Role.role_id)
        .where(User.user_id == user_id)
    ).all()

    if not rows or rows[0].account_status != "ACTIVE":
        raise ApiError("UNAUTHENTICATED", "用户身份不可用")

    user = rows[0]
    role_ids = sorted({row.role_id for row in rows if row.role_id is not None})
    grants = {
        (row.permission_code, row.scope_type, row.scope_id)
        for row in rows
        if row.permission_code is not None
    }
    ordered_grants = sorted(
        grants,
        key=lambda item: (item[0], item[1], item[2] or ""),
    )
    return {
        "userId": user.user_id,
        "displayName": user.display_name,
        "departmentId": user.department_id,
        "accountStatus": user.account_status,
        "roleIds": role_ids,
        "grants": [
            {"permissionCode": code, "scope": {"type": scope_type, "id": scope_id}}
            for code, scope_type, scope_id in ordered_grants
        ],
    }
