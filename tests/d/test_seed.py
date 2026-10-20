"""D 身份初始化测试：在事务内创建独立 schema，结束后整体回滚。"""

import json
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from argon2 import PasswordHasher
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import backend.d.seed as seed_module
from backend.d.models import (
    Base, Department, LoginSession, Permission, Role, RoleGrant, User, UserRole,
)
from backend.shared.db import session_factory as shared_session_factory
from backend.shared.settings import ROOT, Settings


@pytest.fixture
def fixture_data():
    path = ROOT / "tests" / "fixtures" / "bootstrap.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def isolated_db(monkeypatch):
    """所有测试写入独立 schema；seed 的提交只提交内部保存点。"""
    if Settings.load("d").app_env != "test":
        pytest.fail("这些测试要求已有配置 APP_ENV=test。", pytrace=False)

    with shared_session_factory("d")() as session:
        engine = session.get_bind()
    if engine.dialect.name != "postgresql":
        pytest.fail("这些测试要求连接 PostgreSQL。", pytrace=False)

    connection_error_type = None
    try:
        connection = engine.connect()
    except SQLAlchemyError as exc:
        connection_error_type = type(exc).__name__
    if connection_error_type is not None:
        pytest.fail(
            f"D test database connection failed ({connection_error_type}); "
            "check PostgreSQL and local configuration.",
            pytrace=False,
        )

    with connection:
        transaction = connection.begin()
        try:
            # 名称仅由固定前缀和随机十六进制字符组成，不使用外部输入。
            schema = f"d_seed_test_{uuid4().hex}"
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == schema

            scripts = ScriptDirectory(str(ROOT / "backend" / "d" / "migrations"))
            revisions = list(scripts.walk_revisions())
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                for revision in reversed(revisions):
                    revision.module.upgrade()

            factory = sessionmaker(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )

            def test_session_factory(module):
                assert module == "d", "D seed 只能访问 D 数据库"
                return factory

            monkeypatch.setattr(seed_module, "session_factory", test_session_factory)
            yield factory
        finally:
            # PostgreSQL 会同时回滚 schema、建表以及测试数据。
            if transaction.is_active:
                transaction.rollback()


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
