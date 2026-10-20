import json

from argon2 import PasswordHasher

from backend.d.models import (
    Department, Permission, Role, RoleGrant, User, UserRole,
)
from backend.shared.db import session_factory
from backend.shared.settings import ROOT, Settings


def seed():
    """初始化身份测试数据，保留已有记录及其修改。"""
    if Settings.load("d").app_env != "test":
        raise RuntimeError("D seed requires APP_ENV=test")

    fixture_path = ROOT / "tests" / "fixtures" / "bootstrap.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    password_hasher = PasswordHasher()

    departments_added = 0
    permissions_added = 0
    roles_added = 0
    grants_added = 0
    users_added = 0
    user_roles_added = 0

    with session_factory("d").begin() as db:
        for item in fixture["departments"]:
            department_id = item["departmentId"]
            if db.get(Department, department_id) is None:
                db.add(Department(
                    department_id=department_id,
                    name=item["name"],
                ))
                departments_added += 1

        for code in fixture["permissions"]:
            if db.get(Permission, code) is None:
                db.add(Permission(
                    code=code,
                    name=code,
                    allowed_scope_types=fixture["permissionScopeTypes"][code],
                ))
                permissions_added += 1

        for item in fixture["roles"].values():
            role_id = item["roleId"]
            if db.get(Role, role_id) is not None:
                continue

            db.add(Role(
                role_id=role_id,
                name=item["name"],
                status=item["status"],
                version=item["version"],
            ))
            db.flush()
            roles_added += 1

            scope = item["scope"]
            for code in item["permissionCodes"]:
                permission = db.get(Permission, code)
                if permission is None or scope["type"] not in permission.allowed_scope_types:
                    raise ValueError("Invalid role permission or scope in fixture")

                db.add(RoleGrant(
                    role_id=role_id,
                    permission_code=code,
                    scope_type=scope["type"],
                    scope_id=scope["id"],
                ))
                grants_added += 1

        for item in fixture["users"]:
            user_id = item["userId"]
            if db.get(User, user_id) is not None:
                continue

            for role_id in item["roleIds"]:
                role = db.get(Role, role_id)
                if role is None or role.status != "ACTIVE":
                    raise ValueError("Unknown or disabled user role in fixture")

            db.add(User(
                user_id=user_id,
                account=item["account"],
                password_hash=password_hasher.hash(fixture["password"]),
                display_name=item["displayName"],
                department_id=item["departmentId"],
                email=item.get("email"),
                account_status=item["accountStatus"],
                role_assignment_version=1,
            ))
            db.flush()
            users_added += 1

            for role_id in item["roleIds"]:
                db.add(UserRole(user_id=user_id, role_id=role_id))
                user_roles_added += 1

    print(
        f"D catalog seed: departments_added={departments_added}, "
        f"permissions_added={permissions_added}"
    )
    print(
        f"D identity seed: roles_added={roles_added}, "
        f"grants_added={grants_added}, users_added={users_added}, "
        f"user_roles_added={user_roles_added}"
    )