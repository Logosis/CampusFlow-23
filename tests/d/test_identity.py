"""身份汇总的真实数据库测试，复用 D 的隔离测试准备。"""

import pytest
from sqlalchemy import delete, select

import backend.d.seed as seed_module
from backend.d.identity import build_identity
from backend.d.models import Role, RoleGrant, User, UserRole
from backend.shared.errors import ApiError


@pytest.fixture
def seeded_db(isolated_db):
    seed_module.seed()
    return isolated_db


def test_student_identity_has_exact_public_fields(seeded_db, fixture_data):
    expected_user = next(
        item for item in fixture_data["users"] if item["userId"] == "u-student1"
    )
    expected_role = fixture_data["roles"]["student"]
    with seeded_db() as db:
        identity = build_identity(db, "u-student1")
    assert identity == {
        "userId": expected_user["userId"],
        "displayName": expected_user["displayName"],
        "departmentId": expected_user["departmentId"],
        "accountStatus": "ACTIVE",
        "roleIds": ["r-student"],
        "grants": [
            {"permissionCode": code, "scope": {"type": "GLOBAL", "id": None}}
            for code in sorted(expected_role["permissionCodes"])
        ],
    }


def test_screen_grants_keep_their_space_scope(seeded_db):
    with seeded_db() as db:
        identity = build_identity(db, "u-screen")
    assert identity["roleIds"] == ["r-screen"]
    assert identity["grants"]
    assert all(
        grant["scope"] == {"type": "SPACE", "id": "sp-school"}
        for grant in identity["grants"]
    )


def test_unknown_user_is_rejected(seeded_db):
    with seeded_db() as db, pytest.raises(ApiError) as caught:
        build_identity(db, "u-does-not-exist")
    assert caught.value.code == "UNAUTHENTICATED"
    assert caught.value.message == "用户身份不可用"


def test_disabled_user_is_rejected_immediately(seeded_db):
    with seeded_db.begin() as db:
        assert build_identity(db, "u-student1")["accountStatus"] == "ACTIVE"
        db.get(User, "u-student1").account_status = "DISABLED"
        with pytest.raises(ApiError) as caught:
            build_identity(db, "u-student1")
    assert caught.value.code == "UNAUTHENTICATED"
    assert caught.value.message == "用户身份不可用"


def test_disabled_role_stops_granting_without_deleting_assignment(seeded_db):
    with seeded_db.begin() as db:
        assert build_identity(db, "u-student1")["grants"]
        db.get(Role, "r-student").status = "DISABLED"
        identity = build_identity(db, "u-student1")
        assert identity["roleIds"] == []
        assert identity["grants"] == []
        assert db.get(UserRole, ("u-student1", "r-student")) is not None


def test_removing_role_assignment_is_reflected_immediately(seeded_db):
    with seeded_db.begin() as db:
        assert build_identity(db, "u-student1")["roleIds"] == ["r-student"]
        db.execute(delete(UserRole).where(UserRole.user_id == "u-student1"))
        identity = build_identity(db, "u-student1")
    assert identity["accountStatus"] == "ACTIVE"
    assert identity["roleIds"] == []
    assert identity["grants"] == []


def test_revoked_permission_disappears_from_existing_role(seeded_db):
    with seeded_db.begin() as db:
        before = build_identity(db, "u-student1")
        target = before["grants"][0]
        db.execute(delete(RoleGrant).where(
            RoleGrant.role_id == "r-student",
            RoleGrant.permission_code == target["permissionCode"],
        ))
        after = build_identity(db, "u-student1")
    assert after["roleIds"] == before["roleIds"]
    assert after["grants"] == [grant for grant in before["grants"] if grant != target]


def test_duplicate_grants_merge_but_distinct_scopes_are_preserved(seeded_db):
    with seeded_db.begin() as db:
        db.add(Role(role_id="r-test-extra", name="隔离测试角色", status="ACTIVE", version=1))
        db.flush()
        db.add(UserRole(user_id="u-student1", role_id="r-test-extra"))
        for scope_type, scope_id in [
            ("GLOBAL", None), ("DEPARTMENT", "dep-cs"), ("SPACE", "sp-school"),
        ]:
            db.add(RoleGrant(
                role_id="r-test-extra",
                permission_code="d.master.read",
                scope_type=scope_type,
                scope_id=scope_id,
            ))
        identity = build_identity(db, "u-student1")
    assert identity["roleIds"] == ["r-student", "r-test-extra"]
    grants = [item for item in identity["grants"] if item["permissionCode"] == "d.master.read"]
    assert len(grants) == 3
    assert {(item["scope"]["type"], item["scope"]["id"]) for item in grants} == {
        ("GLOBAL", None), ("DEPARTMENT", "dep-cs"), ("SPACE", "sp-school"),
    }


def test_department_can_be_null(seeded_db):
    with seeded_db.begin() as db:
        db.get(User, "u-student1").department_id = None
        identity = build_identity(db, "u-student1")
    assert "departmentId" in identity
    assert identity["departmentId"] is None


def test_active_role_without_grants_remains_in_role_ids(seeded_db):
    with seeded_db.begin() as db:
        db.execute(delete(RoleGrant).where(RoleGrant.role_id == "r-student"))
        identity = build_identity(db, "u-student1")
        assert db.scalar(select(Role.role_id).where(Role.role_id == "r-student")) is not None
    assert identity["roleIds"] == ["r-student"]
    assert identity["grants"] == []
