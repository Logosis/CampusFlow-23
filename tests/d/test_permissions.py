"""D-S04：真实授权数据、范围覆盖、来源认证及 HTTP 契约。"""

from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d import credentials, permissions, sessions
from backend.d import login as login_service
from backend.d import logout as logout_service
import backend.d.seed as seed_module
from backend.d.main import app
from backend.d.models import (
    Base, LoginSession, Permission, Role, RoleGrant, Space,
    SystemGrant, SystemSubject, User, UserRole,
)
from backend.shared.clock import format_time
from backend.shared.errors import ApiError


PATH = "/api/v1/d/check_permission"
GLOBAL = {"type": "GLOBAL", "id": None}
DEP_CS = {"type": "DEPARTMENT", "id": "dep-cs"}
DEP_EE = {"type": "DEPARTMENT", "id": "dep-ee"}
SPACE_CS = {"type": "SPACE", "id": "sp-cs"}
SPACE_SCHOOL = {"type": "SPACE", "id": "sp-school"}
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def configure_client(monkeypatch):
    monkeypatch.setattr(app.state, "settings", replace(
        app.state.settings,
        credentials={module: f"test-service-{module}" for module in ("d", "m1", "m2", "m3")},
    ))


@pytest.fixture
def client(monkeypatch):
    def unexpected_database(module):
        raise AssertionError("Invalid requests must not access the database")

    for service in (permissions, sessions, login_service, logout_service):
        monkeypatch.setattr(service, "session_factory", unexpected_database)
    configure_client(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def permission_db(isolated_db, monkeypatch, tmp_path):
    seed_module.seed()

    def factory(module):
        assert module == "d"
        return isolated_db

    for service in (permissions, sessions, login_service, logout_service):
        monkeypatch.setattr(service, "session_factory", factory)
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.setenv("D_SESSION_SECRET", "ab" * 32)
    clock = tmp_path / "clock.txt"
    clock.write_text(format_time(START), encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_CLOCK_FILE", str(clock))
    return isolated_db


@pytest.fixture
def db_client(permission_db, monkeypatch):
    configure_client(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def headers(mode="SYSTEM", peer="m1", credential=None):
    result = {"X-Service-Credential": f"test-service-{peer}", "X-Actor-Mode": mode}
    if credential is not None:
        result["Authorization"] = f"Bearer {credential}"
    return result


def check(code="d.master.read", target=None, mode="USER", peer="m1", user="u-student1"):
    return permissions.check_permission(
        permission_code=code, target_scope=target, actor_mode=mode,
        source_module=peer, user_id=user if mode == "USER" else None,
    )


def restrict_user(factory, scopes):
    with factory.begin() as db:
        db.execute(delete(RoleGrant).where(RoleGrant.role_id == "r-student"))
        for scope in scopes:
            db.add(RoleGrant(
                role_id="r-student", permission_code="d.master.read",
                scope_type=scope["type"], scope_id=scope["id"],
            ))


def snapshot(factory):
    with factory() as db:
        return {
            table.name: db.execute(select(table).order_by(*table.primary_key.columns)).all()
            for table in Base.metadata.sorted_tables
        }


def assert_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"ok", "data", "error"}
    assert body["ok"] is False and body["data"] is None
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def real_login(client, fixture_data, account="student1"):
    response = client.post("/api/v1/d/login", json={
        "account": account, "password": fixture_data["password"],
        "requestId": f"permission-login-{account}",
    })
    assert response.status_code == 200
    return response.json()["data"]["credential"]


@pytest.mark.parametrize("scope,target,allowed", [
    (GLOBAL, None, True),
    (GLOBAL, GLOBAL, True),
    (GLOBAL, DEP_CS, True),
    (GLOBAL, DEP_EE, True),
    (GLOBAL, SPACE_CS, True),
    (GLOBAL, SPACE_SCHOOL, True),
    (DEP_CS, None, True),
    (DEP_CS, GLOBAL, False),
    (DEP_CS, DEP_CS, True),
    (DEP_CS, DEP_EE, False),
    (DEP_CS, SPACE_CS, True),
    (DEP_CS, SPACE_SCHOOL, False),
    (SPACE_CS, None, True),
    (SPACE_CS, GLOBAL, False),
    (SPACE_CS, DEP_CS, False),
    (SPACE_CS, SPACE_CS, True),
    (SPACE_CS, SPACE_SCHOOL, False),
])
def test_user_scope_coverage_uses_real_space_ownership(permission_db, scope, target, allowed):
    restrict_user(permission_db, [scope])
    result = check(target=target)
    assert result == {
        "subjectType": "USER", "subjectId": "u-student1", "allowed": allowed,
        "allowedScopes": [scope], "reason": "GRANTED" if allowed else "OUT_OF_SCOPE",
    }


def test_empty_role_set_has_no_implicit_department_permission(permission_db):
    with permission_db.begin() as db:
        db.execute(delete(UserRole).where(UserRole.user_id == "u-student1"))
    assert check(target=SPACE_CS) == {
        "subjectType": "USER", "subjectId": "u-student1", "allowed": False,
        "allowedScopes": [], "reason": "NO_GRANT",
    }


def test_all_scopes_are_deduplicated_sorted_and_not_filtered_by_target(permission_db):
    restrict_user(permission_db, [SPACE_SCHOOL, DEP_EE, DEP_CS])
    with permission_db.begin() as db:
        # r-admin 已有同一权限的 GLOBAL；另加与 student 重复的院系授权。
        db.add(UserRole(user_id="u-student1", role_id="r-admin"))
        db.add(RoleGrant(
            role_id="r-admin", permission_code="d.master.read",
            scope_type="DEPARTMENT", scope_id="dep-cs",
        ))
    assert check(target=SPACE_CS)["allowedScopes"] == [DEP_CS, DEP_EE, GLOBAL, SPACE_SCHOOL]
    assert check(target=GLOBAL)["allowed"] is True


def test_role_disable_revoke_and_reassignment_take_effect_on_next_query(permission_db):
    assert check()["allowed"] is True
    with permission_db.begin() as db:
        db.get(Role, "r-student").status = "DISABLED"
    assert check()["allowed"] is False
    with permission_db.begin() as db:
        db.get(Role, "r-student").status = "ACTIVE"
        db.execute(delete(RoleGrant).where(
            RoleGrant.role_id == "r-student", RoleGrant.permission_code == "d.master.read",
        ))
    assert check()["allowed"] is False
    with permission_db.begin() as db:
        db.add(UserRole(user_id="u-student1", role_id="r-admin"))
    assert check()["allowed"] is True
    with permission_db.begin() as db:
        db.execute(delete(UserRole).where(UserRole.user_id == "u-student1"))
    assert check()["allowed"] is False


@pytest.mark.parametrize("user", ["missing-user", "u-student1"])
def test_unavailable_users_cannot_get_permission_result(permission_db, user):
    with permission_db.begin() as db:
        db.get(User, "u-student1").account_status = "DISABLED"
    with pytest.raises(ApiError) as caught:
        check(user=user)
    assert caught.value.code == "UNAUTHENTICATED"


def test_changed_space_department_is_used_but_status_is_left_to_business(permission_db):
    restrict_user(permission_db, [DEP_CS])
    assert check(target=SPACE_CS)["allowed"] is True
    with permission_db.begin() as db:
        space = db.get(Space, "sp-cs")
        space.department_id = "dep-ee"
        space.version += 1
    assert check(target=SPACE_CS)["allowed"] is False
    with permission_db.begin() as db:
        space = db.get(Space, "sp-cs")
        space.department_id = "dep-cs"
        space.enabled = False
        space.registered = False
        space.version += 1
    assert check(target=SPACE_CS)["allowed"] is True


def test_invalid_grant_references_and_catalog_scope_changes_do_not_authorize(permission_db):
    restrict_user(permission_db, [
        {"type": "SPACE", "id": "sp-missing"},
        {"type": "DEPARTMENT", "id": "dep-missing"},
    ])
    assert check()["allowedScopes"] == []
    restrict_user(permission_db, [DEP_CS])
    with permission_db.begin() as db:
        db.get(Permission, "d.master.read").allowed_scope_types = ["GLOBAL"]
    assert check()["reason"] == "NO_GRANT"


@pytest.mark.parametrize("code,target", [
    ("missing.permission", None),
    ("d.master.read", {"type": "DEPARTMENT", "id": "dep-missing"}),
    ("d.master.read", {"type": "SPACE", "id": "sp-missing"}),
])
def test_unknown_permission_or_target_is_not_found_even_with_global_grant(permission_db, code, target):
    with pytest.raises(ApiError) as caught:
        check(code=code, target=target)
    assert caught.value.code == "NOT_FOUND"


@pytest.mark.parametrize("peer,code,allowed", [
    ("m1", "d.master.read", True), ("m1", "d.people.read", False),
    ("m2", "d.people.read", True), ("m3", "d.people.read", True),
    ("m1", "d.rbac.manage", False), ("m2", "d.rbac.manage", False),
    ("m3", "d.config.manage", False),
])
def test_system_uses_only_its_own_persistent_grants(permission_db, peer, code, allowed):
    assert check(code=code, mode="SYSTEM", peer=peer) == {
        "subjectType": "SYSTEM", "subjectId": peer, "allowed": allowed,
        "allowedScopes": [GLOBAL] if allowed else [],
        "reason": "GRANTED" if allowed else "NO_GRANT",
    }


def test_system_scopes_disabled_and_missing_subjects_are_dynamic(permission_db):
    with permission_db.begin() as db:
        db.execute(delete(SystemGrant).where(SystemGrant.module_id == "m2"))
        db.add(SystemGrant(
            module_id="m2", permission_code="d.master.read",
            scope_type="DEPARTMENT", scope_id="dep-cs",
        ))
    assert check(mode="SYSTEM", peer="m2", target=SPACE_CS)["allowed"] is True
    assert check(mode="SYSTEM", peer="m2", target=SPACE_SCHOOL)["reason"] == "OUT_OF_SCOPE"
    with permission_db.begin() as db:
        db.get(SystemSubject, "m2").status = "DISABLED"
    result = check(mode="SYSTEM", peer="m2", target=SPACE_CS)
    assert (result["allowed"], result["allowedScopes"], result["reason"]) == (False, [], "SUBJECT_DISABLED")
    with permission_db.begin() as db:
        db.get(SystemSubject, "m2").status = "ACTIVE"
        db.execute(delete(SystemGrant).where(SystemGrant.module_id == "m2"))
    assert check(mode="SYSTEM", peer="m2")["reason"] == "NO_GRANT"
    with permission_db.begin() as db:
        db.delete(db.get(SystemSubject, "m2"))
    assert check(mode="SYSTEM", peer="m2")["reason"] == "SUBJECT_UNREGISTERED"
    # 停用或撤销 m2 不影响 m1。
    assert check(mode="SYSTEM", peer="m1")["allowed"] is True


def test_permission_queries_write_nothing(permission_db, fixture_data):
    login_service.login(
        account="student1", password=fixture_data["password"], request_id="permission-read-only",
    )
    before = snapshot(permission_db)
    check(target=SPACE_CS)
    check(mode="SYSTEM", target=SPACE_SCHOOL)
    unchanged = snapshot(permission_db) == before
    assert unchanged, "Permission checks must not write data or extend sessions"


@pytest.mark.parametrize("changes,code", [
    ({"source_module": None}, "FORBIDDEN"),
    ({"source_module": "d"}, "FORBIDDEN"),
    ({"source_module": "M1"}, "FORBIDDEN"),
    ({"actor_mode": "unknown"}, "INVALID_ARGUMENT"),
    ({"user_id": None}, "UNAUTHENTICATED"),
    ({"user_id": " "}, "UNAUTHENTICATED"),
    ({"actor_mode": "SYSTEM"}, "INVALID_ARGUMENT"),
])
def test_invalid_trusted_context_rejected_before_database(monkeypatch, changes, code):
    def unexpected(module):
        raise AssertionError("Invalid context reached storage")

    monkeypatch.setattr(permissions, "session_factory", unexpected)
    arguments = dict(
        permission_code="d.master.read", target_scope=None, actor_mode="USER",
        source_module="m1", user_id="u-student1",
    ) | changes
    with pytest.raises(ApiError) as caught:
        permissions.check_permission(**arguments)
    assert caught.value.code == code


@pytest.mark.parametrize("kind", ["connection", "pool-timeout"])
def test_permission_storage_failure_is_unavailable_not_a_denial(monkeypatch, kind):
    def unavailable(module):
        if kind == "connection":
            raise OperationalError("private-query", {}, RuntimeError("private-db-detail"))
        raise SQLAlchemyTimeoutError("private-db-detail")

    monkeypatch.setattr(permissions, "session_factory", unavailable)
    with pytest.raises(ApiError) as caught:
        check(mode="SYSTEM")
    assert caught.value.code == "UNAVAILABLE"
    assert "private" not in caught.value.message


def test_openapi_health_and_output_contract(client):
    health = client.get("/health").json()["data"]
    assert "check_permission" not in health["unfinishedFunctions"]
    assert "maintain_master" in health["unfinishedFunctions"]
    document = client.get("/openapi.json").json()
    assert document["paths"][PATH]["post"]["requestBody"]["required"] is True
    schema = document["components"]["schemas"]["CheckPermissionInput"]
    assert schema["required"] == ["permissionCode"]
    assert set(schema["properties"]) == {"permissionCode", "targetScope"}
    assert schema["additionalProperties"] is False
    output = document["components"]["schemas"]["CheckPermissionOutput"]
    assert set(output["required"]) == {"subjectType", "subjectId", "allowed", "allowedScopes", "reason"}


@pytest.mark.parametrize("payload", [
    {}, {"permissionCode": ""}, {"permissionCode": " "}, {"permissionCode": 1},
    {"permissionCode": True}, {"permissionCode": None},
    {"permissionCode": "d.master.read", "targetScope": None},
    {"permissionCode": "d.master.read", "targetScope": {}},
    {"permissionCode": "d.master.read", "targetScope": {"type": "GLOBAL"}},
    {"permissionCode": "d.master.read", "targetScope": {"type": "GLOBAL", "id": "dep-cs"}},
    {"permissionCode": "d.master.read", "targetScope": {"type": "SPACE", "id": None}},
    {"permissionCode": "d.master.read", "targetScope": {"type": "SPACE", "id": " "}},
    {"permissionCode": "d.master.read", "targetScope": {"type": "SPACE", "id": 1}},
    {"permissionCode": "d.master.read", "targetScope": {"type": "BAD", "id": None}},
    {"permissionCode": "d.master.read", "targetScope": GLOBAL | {"extra": 1}},
    {"permissionCode": "d.master.read", "userId": "u-admin"},
    {"permissionCode": "d.master.read", "sourceModule": "m3"},
    {"permissionCode": "d.master.read", "grants": []},
])
def test_invalid_http_input_rejected_without_storage(client, payload):
    assert_error(client.post(PATH, json=payload, headers=headers()), 422, "INVALID_ARGUMENT")


@pytest.mark.parametrize("request_headers,status,code", [
    ({}, 403, "FORBIDDEN"),
    ({"Authorization": "Bearer " + "a" * 64}, 403, "FORBIDDEN"),
    ({"X-Actor-Mode": "SYSTEM"}, 422, "INVALID_ARGUMENT"),
    ({"X-Service-Credential": "forged"}, 401, "UNAUTHENTICATED"),
    ({"X-Service-Credential": "test-service-m1"}, 422, "INVALID_ARGUMENT"),
    (headers(peer="d"), 403, "FORBIDDEN"),
    (headers(mode="BAD"), 422, "INVALID_ARGUMENT"),
    (headers(mode="USER"), 401, "UNAUTHENTICATED"),
    (headers(mode="USER", credential="short"), 401, "UNAUTHENTICATED"),
    (headers(credential="a" * 64), 422, "INVALID_ARGUMENT"),
])
def test_http_source_and_actor_rules(client, request_headers, status, code):
    assert_error(client.post(PATH, json={"permissionCode": "d.master.read"}, headers=request_headers), status, code)


@pytest.mark.parametrize("peer", ["m1", "m2", "m3"])
def test_http_user_identity_and_system_identity_are_separate(db_client, fixture_data, peer):
    token = real_login(db_client, fixture_data, account="screen")
    response = db_client.post(PATH, json={
        "permissionCode": "d.master.read", "targetScope": SPACE_SCHOOL,
    }, headers=headers(mode="USER", peer=peer, credential=token))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {
        "subjectType": "USER", "subjectId": "u-screen", "allowed": True,
        "allowedScopes": [SPACE_SCHOOL], "reason": "GRANTED",
    }, "error": None}
    denied = db_client.post(PATH, json={
        "permissionCode": "d.master.read", "targetScope": SPACE_CS,
    }, headers=headers(mode="USER", peer=peer, credential=token))
    assert denied.status_code == 200
    assert denied.json()["data"]["allowed"] is False
    system = db_client.post(PATH, json={
        "permissionCode": "d.master.read", "targetScope": SPACE_CS,
    }, headers=headers(peer=peer))
    assert system.status_code == 200
    assert system.json()["data"] == {
        "subjectType": "SYSTEM", "subjectId": peer, "allowed": True,
        "allowedScopes": [GLOBAL], "reason": "GRANTED",
    }
    assert token not in response.text and "password" not in response.text


@pytest.mark.parametrize("state", ["expired", "revoked", "disabled"])
def test_invalid_user_cannot_fall_back_to_system(permission_db, db_client, fixture_data, state):
    token = real_login(db_client, fixture_data)
    with permission_db.begin() as db:
        session = db.scalars(select(LoginSession)).one()
        if state == "expired":
            # 有效区间包含起点、不包含终点。
            from datetime import timedelta
            session.created_at = START - timedelta(hours=8)
            session.expires_at = START
        elif state == "revoked":
            session.revoked_at = START
        else:
            db.get(User, "u-student1").account_status = "DISABLED"
    assert_error(db_client.post(PATH, json={
        "permissionCode": "d.master.read",
    }, headers=headers(mode="USER", credential=token)), 401, "UNAUTHENTICATED")
    # 同一来源在明确 SYSTEM 模式下拥有权限，但不能救活失败的 USER 请求。
    response = db_client.post(PATH, json={"permissionCode": "d.master.read"}, headers=headers())
    assert response.status_code == 200 and response.json()["data"]["allowed"] is True


def test_http_uses_updated_role_grants_without_relogin(permission_db, db_client, fixture_data):
    token = real_login(db_client, fixture_data)
    args = {"json": {"permissionCode": "d.master.read"}, "headers": headers(mode="USER", credential=token)}
    assert db_client.post(PATH, **args).json()["data"]["allowed"] is True
    with permission_db.begin() as db:
        db.get(Role, "r-student").status = "DISABLED"
    response = db_client.post(PATH, **args)
    assert response.status_code == 200
    assert response.json()["data"]["allowedScopes"] == []
    assert response.json()["data"]["allowed"] is False


@pytest.mark.parametrize("payload", [
    {"permissionCode": "missing.permission"},
    {"permissionCode": "d.master.read", "targetScope": {"type": "SPACE", "id": "sp-missing"}},
    {"permissionCode": "d.master.read", "targetScope": {"type": "DEPARTMENT", "id": "dep-missing"}},
])
def test_http_unknown_references_return_404(db_client, payload):
    assert_error(db_client.post(PATH, json=payload, headers=headers()), 404, "NOT_FOUND")


def test_http_storage_outage_does_not_report_allowed_false(client, monkeypatch):
    def unavailable(module):
        raise OperationalError("private-query", {}, RuntimeError("private-db-detail"))

    monkeypatch.setattr(permissions, "session_factory", unavailable)
    response = client.post(PATH, json={"permissionCode": "d.master.read"}, headers=headers())
    assert_error(response, 503, "UNAVAILABLE")
    assert "private" not in response.text
