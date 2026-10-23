"""D-S01 的 HTTP 契约测试：真实路由、真实登录逻辑与隔离 PostgreSQL。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from backend.d import credentials
from backend.d import login as login_service
import backend.d.seed as seed_module
from backend.d.main import app
from backend.d.models import LoginSession, User
from backend.shared.clock import format_time, parse_time


_PATH = "/api/v1/d/login"
_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    # 无效请求测试不允许意外触及开发数据库；数据库用例另绑定隔离连接。
    def unexpected_database_access(module):
        raise AssertionError("This request must be rejected before database access")

    monkeypatch.setattr(login_service, "session_factory", unexpected_database_access)
    settings = replace(
        app.state.settings,
        credentials={module: f"test-service-{module}" for module in ("d", "m1", "m2", "m3")},
    )
    monkeypatch.setattr(app.state, "settings", settings)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def http_login(isolated_db, client, monkeypatch, tmp_path):
    seed_module.seed()

    def factory(module):
        assert module == "d"
        return isolated_db

    monkeypatch.setattr(login_service, "session_factory", factory)
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.setenv("D_SESSION_SECRET", "ab" * 32)
    clock_file = tmp_path / "clock.txt"
    clock_file.write_text(format_time(_START), encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_CLOCK_FILE", str(clock_file))
    return client, isolated_db, clock_file


def login_input(fixture_data, request_id="http-login-1"):
    return {
        "account": "student1",
        "password": fixture_data["password"],
        "requestId": request_id,
    }


def session_count(factory):
    with factory() as db:
        return db.scalar(select(func.count()).select_from(LoginSession))


def assert_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"ok", "data", "error"}
    assert body["ok"] is False
    assert body["data"] is None
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


def test_health_and_openapi_expose_login(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert "login" not in response.json()["data"]["unfinishedFunctions"]
    document = client.get("/openapi.json").json()
    assert "post" in document["paths"][_PATH]
    schema = document["components"]["schemas"]["LoginInput"]
    assert set(schema["required"]) == {"account", "password", "requestId"}
    assert set(schema["properties"]) == {"account", "password", "requestId"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["password"]["writeOnly"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"account": "student1", "password": "test-password"},
        {"account": "student1", "requestId": "test-request"},
        {"password": "test-password", "requestId": "test-request"},
        {"account": "student1", "password": "test-password", "requestId": True},
        {"account": "student1", "password": 123, "requestId": "test-request"},
        {"account": None, "password": "test-password", "requestId": "test-request"},
        {"account": "student1", "password": "test-password", "requestId": "test-request", "extra": 1},
        {"input": {"account": "student1", "password": "test-password", "requestId": "test-request"}},
        {"account": "", "password": "test-password", "requestId": "test-request"},
        {"account": "student1", "password": "", "requestId": "test-request"},
        {"account": "student1", "password": "test-password", "requestId": " "},
    ],
    ids=[
        "empty-body", "missing-request-id", "missing-password", "missing-account",
        "boolean-request-id", "numeric-password", "null-account", "unknown-field",
        "extra-input-wrapper", "empty-account", "empty-password", "blank-request-id",
    ],
)
def test_invalid_input_returns_422_without_accessing_database(client, payload):
    response = client.post(_PATH, json=payload)
    assert_error(response, 422, "INVALID_ARGUMENT")
    assert "test-password" not in response.text


def test_invalid_json_returns_common_error(client):
    response = client.post(_PATH, content="{invalid", headers={"Content-Type": "application/json"})
    assert_error(response, 422, "INVALID_ARGUMENT")


@pytest.mark.parametrize(
    "headers,status,code",
    [
        ({"X-Service-Credential": "forged"}, 401, "UNAUTHENTICATED"),
        ({"X-Actor-Mode": "SYSTEM"}, 422, "INVALID_ARGUMENT"),
        ({"X-Service-Credential": "test-service-m1", "X-Actor-Mode": "USER"}, 403, "FORBIDDEN"),
    ],
    ids=["forged-service", "direct-system-mode", "module-not-authorized-for-login"],
)
def test_login_preserves_shared_source_checks(client, headers, status, code):
    response = client.post(
        _PATH,
        json={"account": "student1", "password": "test-password", "requestId": "source-check"},
        headers=headers,
    )
    assert_error(response, status, code)


def test_real_login_returns_complete_success_envelope(http_login, fixture_data):
    client, factory, _ = http_login
    response = client.post(_PATH, json=login_input(fixture_data))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ok", "data", "error"}
    assert body["ok"] is True
    assert body["error"] is None
    data = body["data"]
    assert set(data) == {"credential", "expiresAt", "identity"}
    assert isinstance(data["credential"], str) and data["credential"]
    assert data["expiresAt"].endswith("Z")
    assert parse_time(data["expiresAt"]) == _START + timedelta(hours=8)
    identity = data["identity"]
    assert set(identity) == {
        "userId", "displayName", "departmentId", "accountStatus", "roleIds", "grants",
    }
    assert identity["userId"] == "u-student1"
    assert identity["departmentId"] == "dep-cs"
    assert identity["accountStatus"] == "ACTIVE"
    assert identity["roleIds"] == ["r-student"]
    assert identity["grants"]
    for grant in identity["grants"]:
        assert set(grant) == {"permissionCode", "scope"}
        assert isinstance(grant["permissionCode"], str)
        assert grant["scope"] == {"type": "GLOBAL", "id": None}
    assert session_count(factory) == 1
    with factory() as db:
        digest_matches = (
            db.scalars(select(LoginSession)).one().credential_hash
            == credentials.credential_digest(data["credential"])
        )
        assert digest_matches


def test_wrong_password_returns_401(http_login, fixture_data):
    client, factory, _ = http_login
    payload = login_input(fixture_data) | {"password": "wrong-test-password"}
    response = client.post(_PATH, json=payload)
    assert_error(response, 401, "AUTH_FAILED")
    assert "wrong-test-password" not in response.text
    assert session_count(factory) == 0


def test_disabled_account_returns_401(http_login, fixture_data):
    client, factory, _ = http_login
    with factory.begin() as db:
        db.get(User, "u-student1").account_status = "DISABLED"
    response = client.post(_PATH, json=login_input(fixture_data))
    assert_error(response, 401, "AUTH_FAILED")
    assert session_count(factory) == 0


def test_duplicate_http_request_returns_original_session(http_login, fixture_data):
    client, factory, clock_file = http_login
    payload = login_input(fixture_data)
    first = client.post(_PATH, json=payload)
    assert first.status_code == 200
    clock_file.write_text(format_time(_START + timedelta(hours=1)), encoding="utf-8")
    repeated = client.post(_PATH, json=payload)
    assert repeated.status_code == 200
    same_token = first.json()["data"]["credential"] == repeated.json()["data"]["credential"]
    assert same_token
    assert first.json()["data"]["expiresAt"] == repeated.json()["data"]["expiresAt"]
    assert session_count(factory) == 1


def test_changed_payload_returns_409(http_login, fixture_data):
    client, factory, _ = http_login
    payload = login_input(fixture_data)
    assert client.post(_PATH, json=payload).status_code == 200
    response = client.post(_PATH, json=payload | {"password": "different-test-password"})
    assert_error(response, 409, "REQUEST_ID_REUSED")
    assert session_count(factory) == 1


def test_expired_request_returns_401_without_extending_session(http_login, fixture_data):
    client, factory, clock_file = http_login
    payload = login_input(fixture_data)
    first = client.post(_PATH, json=payload)
    assert first.status_code == 200
    clock_file.write_text(format_time(_START + timedelta(hours=8)), encoding="utf-8")
    response = client.post(_PATH, json=payload)
    assert_error(response, 401, "AUTH_FAILED")
    assert session_count(factory) == 1
    with factory() as db:
        assert db.scalars(select(LoginSession)).one().expires_at == _START + timedelta(hours=8)


def test_missing_key_returns_503_and_does_not_persist_session(http_login, fixture_data, monkeypatch):
    client, factory, _ = http_login
    monkeypatch.delenv("D_SESSION_SECRET")
    response = client.post(_PATH, json=login_input(fixture_data))
    assert_error(response, 503, "UNAVAILABLE")
    assert session_count(factory) == 0
