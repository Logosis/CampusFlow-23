"""D 身份初始化测试；数据库隔离准备由 conftest.py 提供。"""

import json

import pytest
from argon2 import PasswordHasher
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

import backend.d.seed as seed_module
from backend.d.models import (
    Base, Department, LoginSession, Permission, Role, RoleGrant, User, UserRole,
)


def snapshot(factory):
    with factory() as db:
        return {
            table.name: db.execute(
                select(table).order_by(*table.primary_key.columns)
            ).all()
            for table in Base.metadata.sorted_tables
        }


def test_seed_creates_expected_identity_data(isolated_db, fixture_data):
    seed_module.seed()
    expected_counts = {
        Department: len(fixture_data["departments"]),
        Permission: len(fixture_data["permissions"]),
        Role: len(fixture_data["roles"]),
        RoleGrant: sum(
            len(item["permissionCodes"]) for item in fixture_data["roles"].values()
        ),
        User: len(fixture_data["users"]),
        UserRole: sum(len(item["roleIds"]) for item in fixture_data["users"]),
        LoginSession: 0,
    }
    hasher = PasswordHasher()
    stored_hashes = set()

    with isolated_db() as db:
        for model, expected in expected_counts.items():
            assert db.scalar(select(func.count()).select_from(model)) == expected

        for item in fixture_data["departments"]:
            assert db.get(Department, item["departmentId"]).name == item["name"]

        for code in fixture_data["permissions"]:
            permission = db.get(Permission, code)
            assert permission.allowed_scope_types == fixture_data["permissionScopeTypes"][code]

        for item in fixture_data["roles"].values():
            role = db.get(Role, item["roleId"])
            assert (role.name, role.status, role.version) == (
                item["name"], item["status"], item["version"],
            )
            grants = set(db.execute(
                select(RoleGrant.permission_code, RoleGrant.scope_type, RoleGrant.scope_id)
                .where(RoleGrant.role_id == role.role_id)
            ).all())
            expected_grants = {
                (code, item["scope"]["type"], item["scope"]["id"])
                for code in item["permissionCodes"]
            }
            assert grants == expected_grants

        for item in fixture_data["users"]:
            user = db.get(User, item["userId"])
            assert (user.account, user.display_name, user.department_id) == (
                item["account"], item["displayName"], item["departmentId"],
            )
            assert user.email == item.get("email")
            assert user.account_status == item["accountStatus"]
            assert user.role_assignment_version == 1
            is_argon2_hash = user.password_hash.startswith("$argon2id$")
            assert is_argon2_hash, "密码必须保存为 Argon2id 哈希"
            verified = hasher.verify(user.password_hash, fixture_data["password"])
            assert verified
            stored_hashes.add(user.password_hash)
            role_ids = set(db.scalars(
                select(UserRole.role_id).where(UserRole.user_id == user.user_id)
            ))
            assert role_ids == set(item["roleIds"])

    assert len(stored_hashes) == len(fixture_data["users"])


def test_seed_repeat_changes_nothing(isolated_db):
    seed_module.seed()
    before = snapshot(isolated_db)
    seed_module.seed()
    unchanged = snapshot(isolated_db) == before
    assert unchanged, "重复初始化不应改写已有记录或重新生成密码哈希"


def test_seed_preserves_existing_edits_and_revoked_grants(isolated_db):
    seed_module.seed()
    with isolated_db.begin() as db:
        db.get(Department, "dep-cs").name = "测试中修改的院系名称"
        permission = db.get(Permission, "d.master.read")
        permission.name = "测试中修改的权限名称"
        permission.allowed_scope_types = ["GLOBAL"]
        role = db.get(Role, "r-student")
        role.status = "DISABLED"
        role.version = 2
        db.execute(delete(RoleGrant).where(RoleGrant.role_id == "r-student"))
        user = db.get(User, "u-student1")
        user.account_status = "DISABLED"
        user.password_hash = PasswordHasher().hash("changed-only-in-isolated-test")
        user.role_assignment_version = 2
        db.execute(delete(UserRole).where(UserRole.user_id == "u-student1"))

    before = snapshot(isolated_db)
    seed_module.seed()
    unchanged = snapshot(isolated_db) == before
    assert unchanged, "初始化不应恢复撤销的授权、重置密码或覆盖已有修改"


def test_seed_rolls_back_when_fixture_is_invalid(
    isolated_db, fixture_data, monkeypatch, tmp_path,
):
    fixture_data["roles"]["screen"]["permissionCodes"].append("unknown.permission")
    target = tmp_path / "tests" / "fixtures" / "bootstrap.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(fixture_data), encoding="utf-8")
    monkeypatch.setattr(seed_module, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="Invalid role permission or scope"):
        seed_module.seed()

    with isolated_db() as db:
        for table in Base.metadata.sorted_tables:
            assert db.scalar(select(func.count()).select_from(table)) == 0


def test_seed_rejects_non_test_environment(isolated_db, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="APP_ENV=test"):
        seed_module.seed()
    with isolated_db() as db:
        for table in Base.metadata.sorted_tables:
            assert db.scalar(select(func.count()).select_from(table)) == 0


def test_database_rejects_duplicate_global_grant(isolated_db):
    seed_module.seed()
    with pytest.raises(IntegrityError) as error:
        with isolated_db.begin() as db:
            db.add(RoleGrant(
                role_id="r-student",
                permission_code="m1.reservation.create",
                scope_type="GLOBAL",
                scope_id=None,
            ))
    assert error.value.orig.sqlstate == "23505"
