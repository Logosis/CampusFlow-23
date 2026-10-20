"""D-S02：真实登录会话、身份刷新与 HTTP 契约，数据库复用隔离 schema。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d import credentials, sessions
from backend.d import login as login_service
import backend.d.seed as seed_module
from backend.d.main import app
from backend.d.models import LoginSession, Role, RoleGrant, User, UserRole
from backend.shared.clock import format_time
from backend.shared.errors import ApiError


_PATH = "/api/v1/d/get_identity"
_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    def unexpected_database_access(module):
        raise AssertionError("This request must be rejected before database access")

    monkeypatch.setattr(sessions, "session_factory", unexpected_database_access)
    monkeypatch.setattr(login_service, "session_factory", unexpected_database_access)
    settings = replace(
        app.state.settings,
        credentials={module: f"test-service-{module}" for module in ("d", "m1", "m2", "m3")},
    )
    monkeypatch.setattr(app.state, "settings", settings)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def authenticated(isolated_db, client, fixture_data, monkeypatch, tmp_path):
    seed_module.seed()

    def factory(module):
        assert module == "d"
        return isolated_db

    monkeypatch.setattr(login_service, "session_factory", factory)
    monkeypatch.setattr(sessions, "session_factory", factory)
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.setenv("D_SESSION_SECRET", "ab" * 32)
    clock_file = tmp_path / "clock.txt"
    clock_file.write_text(format_time(_START), encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_CLOCK_FILE", str(clock_file))
    response = client.post("/api/v1/d/login", json={
        "account": "student1",
        "password": fixture_data["password"],
        "requestId": "identity-login-1",
    })
    assert response.status_code == 200
    data = response.json()["data"]
    return client, isolated_db, clock_file, data


def auth_headers(data):
    return {"Authorization": f"Bearer {data['credential']}"}


def assert_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"ok", "data", "error"}
    assert body["ok"] is False
    assert body["data"] is None
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


@pytest.mark.parametrize("token", [None, "", "short", "A" * 64, "a" * 63 + "g", "汉" * 64])
def test_malformed_credential_rejected_before_database(monkeypatch, token):
    def unexpected_database_access(module):
        raise AssertionError("Malformed credential must not reach the database")

    monkeypatch.setattr(sessions, "session_factory", unexpected_database_access)
    with pytest.raises(ApiError) as caught:
        sessions.get_identity_for_credential(token)
    assert caught.value.code == "UNAUTHENTICATED"


@pytest.mark.parametrize("kind", ["connection", "pool-timeout"])
def test_database_failure_is_unavailable_without_leaking_details(monkeypatch, kind):
    def unavailable_database(module):
        if kind == "connection":
            raise OperationalError("private-query", {}, RuntimeError("private-db-detail"))
        raise SQLAlchemyTimeoutError("private-db-detail")

    monkeypatch.setattr(sessions, "session_factory", unavailable_database)
    with pytest.raises(ApiError) as caught:
        sessions.get_identity_for_credential("a" * 64)
    assert caught.value.code == "UNAVAILABLE"
    assert "private" not in caught.value.message


def test_health_and_openapi_register_identity(client):
    health = client.get("/health").json()["data"]
    assert "login" not in health["unfinishedFunctions"]
    assert "get_identity" not in health["unfinishedFunctions"]
    assert "manage_roles" in health["unfinishedFunctions"]
    document = client.get("/openapi.json").json()
    operation = document["paths"][_PATH]["post"]
    assert operation["requestBody"]["required"] is True
    schema = document["components"]["schemas"]["IdentityInput"]
    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False
    assert set(document["components"]["schemas"]["IdentityOutput"]["required"]) == {
        "userId", "displayName", "departmentId", "accountStatus", "roleIds", "grants",
    }


@pytest.mark.parametrize(
    "headers,status,code",
    [
        ({}, 401, "UNAUTHENTICATED"),
        ({"Authorization": "Basic invalid"}, 401, "UNAUTHENTICATED"),
        ({"Authorization": "Bearer "}, 401, "UNAUTHENTICATED"),
        ({"Authorization": "Bearer short"}, 401, "UNAUTHENTICATED"),
        ({"X-Actor-Mode": "SYSTEM"}, 422, "INVALID_ARGUMENT"),
        ({"X-Service-Credential": "forged"}, 401, "UNAUTHENTICATED"),
        ({"X-Service-Credential": "test-service-m1", "X-Actor-Mode": "USER"}, 401, "UNAUTHENTICATED"),
        ({"X-Service-Credential": "test-service-m1", "X-Actor-Mode": "SYSTEM"}, 403, "FORBIDDEN"),
        ({
            "X-Service-Credential": "test-service-m1",
            "X-Actor-Mode": "SYSTEM",
            "Authorization": "Bearer short",
        }, 422, "INVALID_ARGUMENT"),
        ({"X-Service-Credential": "test-service-d", "X-Actor-Mode": "USER"}, 403, "FORBIDDEN"),
        ({"X-Service-Credential": "test-service-m2"}, 422, "INVALID_ARGUMENT"),
    ],
    ids=[
        "missing-token", "wrong-scheme", "empty-token", "malformed-token",
        "direct-system", "forged-module", "module-missing-user", "system-forbidden",
        "system-carries-token", "wrong-source", "module-missing-mode",
    ],
)
def test_authentication_and_source_checks(client, headers, status, code):
    assert_error(client.post(_PATH, json={}, headers=headers), status, code)


def test_direct_query_returns_exact_current_identity_without_extending_session(authenticated):
    client, factory, clock_file, login_data = authenticated
    clock_file.write_text(format_time(_START + timedelta(hours=1)), encoding="utf-8")
    response = client.post(_PATH, json={}, headers=auth_headers(login_data))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": login_data["identity"], "error": None}
    leaked = login_data["credential"] in response.text
    assert not leaked
    assert "password" not in response.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 1
        record = db.scalars(select(LoginSession)).one()
        assert record.created_at == _START
        assert record.expires_at == _START + timedelta(hours=8)
        assert record.revoked_at is None


@pytest.mark.parametrize("peer", ["m1", "m2", "m3"])
def test_trusted_module_can_forward_real_user_credential(authenticated, peer):
    client, _, _, login_data = authenticated
    headers = auth_headers(login_data) | {
        "X-Service-Credential": f"test-service-{peer}",
        "X-Actor-Mode": "USER",
    }
    response = client.post(_PATH, json={}, headers=headers)
    assert response.status_code == 200
    assert response.json()["data"] == login_data["identity"]


@pytest.mark.parametrize("payload", [
    {"userId": "u-admin"},
    {"credential": "forged"},
    {"input": {}},
    [],
])
def test_request_cannot_choose_another_user_or_wrap_input(authenticated, payload):
    client, _, _, login_data = authenticated
    assert_error(
        client.post(_PATH, json=payload, headers=auth_headers(login_data)),
        422, "INVALID_ARGUMENT",
    )


def test_missing_body_is_rejected(authenticated):
    client, _, _, login_data = authenticated
    assert_error(client.post(_PATH, headers=auth_headers(login_data)), 422, "INVALID_ARGUMENT")


def test_unknown_well_formed_credential_is_rejected(authenticated):
    client, _, _, login_data = authenticated
    token = login_data["credential"]
    changed = ("0" if token[0] != "0" else "1") + token[1:]
    response = client.post(_PATH, json={}, headers={"Authorization": f"Bearer {changed}"})
    assert_error(response, 401, "UNAUTHENTICATED")


@pytest.mark.parametrize(
    "offset,accepted",
    [
        (timedelta(microseconds=-1), False),
        (timedelta(0), True),
        (timedelta(hours=8, microseconds=-1), True),
        (timedelta(hours=8), False),
        (timedelta(hours=9), False),
    ],
    ids=["before-creation", "at-creation", "before-expiry", "at-expiry", "after-expiry"],
)
def test_session_validity_boundaries(authenticated, offset, accepted):
    client, _, clock_file, login_data = authenticated
    clock_file.write_text(format_time(_START + offset), encoding="utf-8")
    response = client.post(_PATH, json={}, headers=auth_headers(login_data))
    if accepted:
        assert response.status_code == 200
    else:
        assert_error(response, 401, "UNAUTHENTICATED")


def test_revoked_session_rejected_on_next_request(authenticated):
    client, factory, _, login_data = authenticated
    headers = auth_headers(login_data)
    assert client.post(_PATH, json={}, headers=headers).status_code == 200
    with factory.begin() as db:
        db.scalars(select(LoginSession)).one().revoked_at = _START
    assert_error(client.post(_PATH, json={}, headers=headers), 401, "UNAUTHENTICATED")


def test_disabled_account_rejected_on_next_request(authenticated):
    client, factory, _, login_data = authenticated
    headers = auth_headers(login_data)
    assert client.post(_PATH, json={}, headers=headers).status_code == 200
    with factory.begin() as db:
        db.get(User, "u-student1").account_status = "DISABLED"
    assert_error(client.post(_PATH, json={}, headers=headers), 401, "UNAUTHENTICATED")


@pytest.mark.parametrize("change", ["disable-role", "remove-assignment", "remove-grant"])
def test_role_and_permission_changes_are_visible_without_relogin(authenticated, change):
    client, factory, _, login_data = authenticated
    headers = auth_headers(login_data)
    before = client.post(_PATH, json={}, headers=headers).json()["data"]
    removed = before["grants"][0]
    with factory.begin() as db:
        if change == "disable-role":
            db.get(Role, "r-student").status = "DISABLED"
        elif change == "remove-assignment":
            db.execute(delete(UserRole).where(UserRole.user_id == "u-student1"))
        else:
            db.execute(delete(RoleGrant).where(
                RoleGrant.role_id == "r-student",
                RoleGrant.permission_code == removed["permissionCode"],
            ))
    response = client.post(_PATH, json={}, headers=headers)
    assert response.status_code == 200
    after = response.json()["data"]
    assert after["accountStatus"] == "ACTIVE"
    if change == "remove-grant":
        assert after["roleIds"] == before["roleIds"]
        assert after["grants"] == [grant for grant in before["grants"] if grant != removed]
    else:
        assert after["roleIds"] == []
        assert after["grants"] == []


def test_profile_changes_and_null_department_are_current(authenticated):
    client, factory, _, login_data = authenticated
    with factory.begin() as db:
        user = db.get(User, "u-student1")
        user.display_name = "Updated student"
        user.department_id = None
    response = client.post(_PATH, json={}, headers=auth_headers(login_data))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["displayName"] == "Updated student"
    assert "departmentId" in data and data["departmentId"] is None


def test_database_failure_returns_503_not_empty_identity(client, monkeypatch):
    def unavailable_database(module):
        raise SQLAlchemyTimeoutError("private-connection-detail")

    monkeypatch.setattr(sessions, "session_factory", unavailable_database)
    response = client.post(_PATH, json={}, headers={"Authorization": f"Bearer {'a' * 64}"})
    assert_error(response, 503, "UNAVAILABLE")
    assert "private-connection-detail" not in response.text
