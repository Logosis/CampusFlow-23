"""权限判断的数据基础：真实迁移、空间归属、后台授权与重复初始化。"""

import json
import re

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

import backend.d.seed as seed_module
from backend.d.models import Base, Space, SystemGrant, SystemSubject
from backend.shared.settings import ROOT


def snapshot(factory, exclude=()):
    with factory() as db:
        return {
            table.name: db.execute(
                select(table).order_by(*table.primary_key.columns)
            ).all()
            for table in Base.metadata.sorted_tables
            if table.name not in exclude
        }


def use_fixture(fixture, tmp_path, monkeypatch):
    target = tmp_path / "tests" / "fixtures" / "bootstrap.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(fixture), encoding="utf-8")
    monkeypatch.setattr(seed_module, "ROOT", tmp_path)


def assert_empty(factory):
    with factory() as db:
        for table in Base.metadata.sorted_tables:
            assert db.scalar(select(func.count()).select_from(table)) == 0


def test_seed_creates_exact_shared_spaces_and_system_grants(isolated_db, fixture_data):
    seed_module.seed()
    with isolated_db() as db:
        assert db.scalar(select(func.count()).select_from(Space)) == len(fixture_data["spaces"])
        assert db.scalar(select(func.count()).select_from(SystemSubject)) == len(fixture_data["systemGrants"])
        assert db.scalar(select(func.count()).select_from(SystemGrant)) == sum(
            len(item["permissionCodes"]) for item in fixture_data["systemGrants"].values()
        )
        for item in fixture_data["spaces"]:
            space = db.get(Space, item["spaceId"])
            assert {
                "spaceId": space.space_id,
                "name": space.name,
                "location": space.location,
                "isStudyRoom": space.is_study_room,
                "ownerKind": space.owner_kind,
                "departmentId": space.department_id,
                "registered": space.registered,
                "enabled": space.enabled,
                "version": space.version,
            } == item
        for module_id, item in fixture_data["systemGrants"].items():
            assert db.get(SystemSubject, module_id).status == "ACTIVE"
            actual = set(db.execute(
                select(SystemGrant.permission_code, SystemGrant.scope_type, SystemGrant.scope_id)
                .where(SystemGrant.module_id == module_id)
            ).all())
            assert actual == {
                (code, item["scope"]["type"], item["scope"]["id"])
                for code in item["permissionCodes"]
            }
            assert not {row[0] for row in actual}.intersection({
                "d.rbac.manage", "d.config.manage", "d.master.manage",
            })


def test_repeat_preserves_space_changes_disabled_subjects_and_revoked_grants(isolated_db):
    seed_module.seed()
    with isolated_db.begin() as db:
        space = db.get(Space, "sp-cs")
        space.name = "Changed space"
        space.department_id = "dep-ee"
        space.registered = False
        space.enabled = False
        space.version = 2
        db.get(SystemSubject, "m2").status = "DISABLED"
        db.execute(delete(SystemGrant).where(SystemGrant.module_id == "m2"))
    before = snapshot(isolated_db)
    seed_module.seed()
    unchanged = snapshot(isolated_db) == before
    assert unchanged, "Seed must not restore grants or overwrite existing records"


@pytest.mark.parametrize("invalid", [
    "module", "permission", "scope-type", "global-id", "unknown-department", "unknown-space",
])
def test_invalid_system_fixture_rolls_back_every_table(
    isolated_db, fixture_data, tmp_path, monkeypatch, invalid,
):
    item = fixture_data["systemGrants"]["m3"]
    if invalid == "module":
        fixture_data["systemGrants"]["unknown"] = fixture_data["systemGrants"].pop("m3")
    elif invalid == "permission":
        item["permissionCodes"].append("unknown.permission")
    elif invalid == "scope-type":
        item["scope"] = {"type": "UNKNOWN", "id": "scope-id"}
    elif invalid == "global-id":
        item["scope"] = {"type": "GLOBAL", "id": "dep-cs"}
    elif invalid == "unknown-department":
        item["scope"] = {"type": "DEPARTMENT", "id": "dep-missing"}
    else:
        item["scope"] = {"type": "SPACE", "id": "sp-missing"}
    use_fixture(fixture_data, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Invalid system"):
        seed_module.seed()
    assert_empty(isolated_db)


@pytest.mark.parametrize("scope", [
    {"type": "DEPARTMENT", "id": "dep-cs"},
    {"type": "SPACE", "id": "sp-school"},
])
def test_system_scoped_grants_reference_real_shared_data(
    isolated_db, fixture_data, tmp_path, monkeypatch, scope,
):
    fixture_data["systemGrants"]["m2"]["scope"] = scope
    use_fixture(fixture_data, tmp_path, monkeypatch)
    seed_module.seed()
    with isolated_db() as db:
        scopes = set(db.execute(
            select(SystemGrant.scope_type, SystemGrant.scope_id)
            .where(SystemGrant.module_id == "m2")
        ).all())
        assert scopes == {(scope["type"], scope["id"])}


@pytest.mark.parametrize("scope", [
    {"type": "SPACE", "id": "sp-missing"},
    {"type": "DEPARTMENT", "id": "dep-missing"},
])
def test_new_user_role_grants_also_require_existing_scope_references(
    isolated_db, fixture_data, tmp_path, monkeypatch, scope,
):
    fixture_data["roles"]["student"]["scope"] = scope
    use_fixture(fixture_data, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Invalid role permission or scope"):
        seed_module.seed()
    assert_empty(isolated_db)


@pytest.mark.parametrize("changes,sqlstate", [
    ({"owner_kind": "SCHOOL", "department_id": "dep-cs"}, "23514"),
    ({"owner_kind": "DEPARTMENT", "department_id": None}, "23514"),
    ({"owner_kind": "DEPARTMENT", "department_id": "dep-missing"}, "23503"),
    ({"version": 0}, "23514"),
    ({"name": " "}, "23514"),
    ({"location": " "}, "23514"),
])
def test_space_constraints_are_enforced_by_real_migration(isolated_db, changes, sqlstate):
    seed_module.seed()
    with pytest.raises(IntegrityError) as caught:
        with isolated_db.begin() as db:
            db.add(Space(**({
                "space_id": "sp-invalid",
                "name": "Invalid test space",
                "location": "Test location",
                "is_study_room": True,
                "owner_kind": "SCHOOL",
                "department_id": None,
                "registered": True,
                "enabled": True,
                "version": 1,
            } | changes)))
    assert caught.value.orig.sqlstate == sqlstate


@pytest.mark.parametrize("module_id,status", [("unknown", "ACTIVE"), ("m1", "UNKNOWN")])
def test_system_subject_constraints_are_enforced(isolated_db, module_id, status):
    with pytest.raises(IntegrityError) as caught:
        with isolated_db.begin() as db:
            db.add(SystemSubject(module_id=module_id, status=status))
    assert caught.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("changes,sqlstate", [
    ({}, "23505"),
    ({"module_id": "missing"}, "23503"),
    ({"permission_code": "unknown.permission"}, "23503"),
    ({"scope_type": "GLOBAL", "scope_id": "dep-cs"}, "23514"),
    ({"scope_type": "DEPARTMENT", "scope_id": None}, "23514"),
    ({"scope_type": "SPACE", "scope_id": " "}, "23514"),
])
def test_system_grant_constraints_include_unique_global_scope(isolated_db, changes, sqlstate):
    seed_module.seed()
    with pytest.raises(IntegrityError) as caught:
        with isolated_db.begin() as db:
            db.add(SystemGrant(**({
                "module_id": "m1",
                "permission_code": "d.master.read",
                "scope_type": "GLOBAL",
                "scope_id": None,
            } | changes)))
    assert caught.value.orig.sqlstate == sqlstate


def test_new_migration_preserves_existing_identity_data_and_can_be_seeded(isolated_db):
    seed_module.seed()
    identity_tables = {
        "departments", "users", "roles", "permissions", "role_grants", "user_roles", "login_sessions",
    }
    new_tables = set(Base.metadata.tables) - identity_tables
    before = snapshot(isolated_db, exclude=new_tables)
    scripts = ScriptDirectory(str(ROOT / "backend" / "d" / "migrations"))
    # 在完整的当前迁移链中，先撤销依赖空间的后续表，再撤销本次三表。
    revisions = list(scripts.walk_revisions(base="d_09c1a686d136", head="head"))
    assert revisions[-1].revision == "d_09c1a686d136"
    # 仅操作普通测试 fixture 的随机 schema，模拟已有身份数据上的升级。
    with isolated_db.begin() as db:
        schema = db.scalar(text("SELECT current_schema()"))
        assert re.fullmatch(r"d_seed_test_[0-9a-f]{32}", schema)
        with Operations.context(MigrationContext.configure(db.connection())):
            for revision in revisions:
                revision.module.downgrade()
            for revision in reversed(revisions):
                revision.module.upgrade()
        for name in new_tables:
            assert db.scalar(select(func.count()).select_from(Base.metadata.tables[name])) == 0
    unchanged = snapshot(isolated_db, exclude=new_tables) == before
    assert unchanged
    seed_module.seed()
    unchanged = snapshot(isolated_db, exclude=new_tables) == before
    assert unchanged
    with isolated_db() as db:
        assert db.scalar(select(func.count()).select_from(Space)) == 2
        assert db.scalar(select(func.count()).select_from(SystemSubject)) == 3
        assert db.scalar(select(func.count()).select_from(SystemGrant)) == 17
