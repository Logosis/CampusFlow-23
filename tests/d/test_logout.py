"""D-S03 的真实 HTTP、持久化、重复及故障检查。"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d import credentials, sessions
from backend.d import login as login_service
from backend.d import logout as logout_service
import backend.d.seed as seed_module
from backend.d.main import app
from backend.d.models import LoginSession, User
from backend.shared.clock import format_time
from backend.shared.errors import ApiError


_PATH = "/api/v1/d/logout"
_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    def unexpected_database_access(module):
        raise AssertionError("This request must not access the development database")

    for service in (login_service, logout_service, sessions):
        monkeypatch.setattr(service, "session_factory", unexpected_database_access)
    monkeypatch.setattr(app.state, "settings", replace(
        app.state.settings,
        credentials={module: f"test-service-{module}" for module in ("d", "m1", "m2", "m3")},
    ))
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def logged_in(isolated_db, client, fixture_data, monkeypatch, tmp_path):
    seed_module.seed()

    def factory(module):
        assert module == "d"
        return isolated_db

    for service in (login_service, logout_service, sessions):
        monkeypatch.setattr(service, "session_factory", factory)
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.setenv("D_SESSION_SECRET", "ab" * 32)
    clock_file = tmp_path / "clock.txt"
    clock_file.write_text(format_time(_START), encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_CLOCK_FILE", str(clock_file))
    payload = {
        "account": "student1",
        "password": fixture_data["password"],
        "requestId": "logout-login-1",
    }
    response = client.post("/api/v1/d/login", json=payload)
    assert response.status_code == 200
    return client, isolated_db, clock_file, payload, response.json()["data"]


def headers(data):
    return {"Authorization": f"Bearer {data['credential']}"}


def assert_success(response):
    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"loggedOut": True}, "error": None}


def assert_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"ok", "data", "error"}
    assert body["ok"] is False and body["data"] is None
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


@pytest.mark.parametrize("request_id", [None, "", " ", 123, True])
def test_invalid_request_id_rejected_before_database(monkeypatch, request_id):
    def unexpected_database_access(module):
        raise AssertionError("Invalid requestId must not access the database")

    monkeypatch.setattr(logout_service, "session_factory", unexpected_database_access)
    with pytest.raises(ApiError) as caught:
        logout_service.logout("a" * 64, request_id)
    assert caught.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("token", [None, "", "short", "A" * 64])
def test_malformed_token_rejected_before_database(monkeypatch, token):
    def unexpected_database_access(module):
        raise AssertionError("Malformed credential must not access the database")

    monkeypatch.setattr(logout_service, "session_factory", unexpected_database_access)
    with pytest.raises(ApiError) as caught:
        logout_service.logout(token, "logout-1")
    assert caught.value.code == "UNAUTHENTICATED"


@pytest.mark.parametrize("phase", ["connect", "commit"])
def test_database_failure_does_not_report_success(monkeypatch, phase):
    monkeypatch.setattr(logout_service, "utc_now", lambda: _START)

    class FailingTransaction:
        def __enter__(self):
            if phase == "connect":
                raise OperationalError("private-query", {}, RuntimeError("private-detail"))
            return self

        def execute(self, statement):
            pass

        def __exit__(self, exc_type, exc, traceback):
            raise SQLAlchemyTimeoutError("private-detail")

    class FailingFactory:
        def begin(self):
            return FailingTransaction()

    monkeypatch.setattr(logout_service, "session_factory", lambda module: FailingFactory())
    with pytest.raises(ApiError) as caught:
        logout_service.logout("a" * 64, "logout-1")
    assert caught.value.code == "UNAVAILABLE"
    assert "private" not in caught.value.message


def test_health_and_openapi_register_logout(client):
    missing = client.get("/health").json()["data"]["unfinishedFunctions"]
    assert not {"login", "get_identity", "logout"}.intersection(missing)
    assert "check_permission" not in missing
    document = client.get("/openapi.json").json()
    assert "post" in document["paths"][_PATH]
    schema = document["components"]["schemas"]["LogoutInput"]
    assert set(schema["required"]) == {"requestId"}
    assert set(schema["properties"]) == {"requestId"}
    assert schema["additionalProperties"] is False
    output = document["components"]["schemas"]["LogoutOutput"]
    assert output["required"] == ["loggedOut"]
    assert output["properties"]["loggedOut"]["type"] == "boolean"


@pytest.mark.parametrize("payload", [
    {}, {"requestId": ""}, {"requestId": " "}, {"requestId": True},
    {"requestId": 123}, {"requestId": None},
    {"requestId": "logout-1", "userId": "u-admin"},
    {"input": {"requestId": "logout-1"}},
])
def test_invalid_http_input_cannot_revoke_a_session(client, payload):
    response = client.post(_PATH, json=payload, headers={"Authorization": f"Bearer {'a' * 64}"})
    assert_error(response, 422, "INVALID_ARGUMENT")


@pytest.mark.parametrize(
    "auth,status,code",
    [
        ({}, 401, "UNAUTHENTICATED"),
        ({"Authorization": "Basic invalid"}, 401, "UNAUTHENTICATED"),
        ({"Authorization": "Bearer short"}, 401, "UNAUTHENTICATED"),
        ({"Authorization": f"Bearer {'a' * 64}", "X-Actor-Mode": "SYSTEM"}, 422, "INVALID_ARGUMENT"),
        ({"X-Service-Credential": "forged"}, 401, "UNAUTHENTICATED"),
        ({"X-Service-Credential": "test-service-m1", "X-Actor-Mode": "USER"}, 403, "FORBIDDEN"),
    ],
)
def test_logout_requires_original_credential_and_direct_source(client, auth, status, code):
    assert_error(client.post(_PATH, json={"requestId": "logout-1"}, headers=auth), status, code)


def test_logout_persists_revocation_and_blocks_identity_and_original_login(logged_in):
    client, factory, _, login_payload, data = logged_in
    auth = headers(data)
    assert client.post("/api/v1/d/get_identity", json={}, headers=auth).status_code == 200
    assert_success(client.post(_PATH, json={"requestId": "logout-1"}, headers=auth))
    assert_error(client.post("/api/v1/d/get_identity", json={}, headers=auth), 401, "UNAUTHENTICATED")
    assert_error(client.post("/api/v1/d/login", json=login_payload), 401, "AUTH_FAILED")
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 1
        record = db.scalars(select(LoginSession)).one()
        assert record.revoked_at == _START
        assert record.expires_at == _START + timedelta(hours=8)
        assert record.created_at == _START


def test_repeated_logout_preserves_first_revocation_time(logged_in):
    client, factory, clock_file, _, data = logged_in
    auth = headers(data)
    assert_success(client.post(_PATH, json={"requestId": "logout-1"}, headers=auth))
    clock_file.write_text(format_time(_START + timedelta(hours=1)), encoding="utf-8")
    for request_id in ("logout-1", "logout-new-id"):
        assert_success(client.post(_PATH, json={"requestId": request_id}, headers=auth))
    with factory() as db:
        assert db.scalars(select(LoginSession)).one().revoked_at == _START


@pytest.mark.parametrize("second_account", ["student1", "student2"])
def test_logout_only_affects_current_session_and_request_id_is_scoped_by_credential(
    logged_in, fixture_data, second_account,
):
    client, factory, _, _, first = logged_in
    response = client.post("/api/v1/d/login", json={
        "account": second_account,
        "password": fixture_data["password"],
        "requestId": "logout-login-2",
    })
    assert response.status_code == 200
    second = response.json()["data"]
    assert_success(client.post(_PATH, json={"requestId": "same-logout-id"}, headers=headers(first)))
    response = client.post("/api/v1/d/get_identity", json={}, headers=headers(second))
    assert response.status_code == 200
    assert response.json()["data"] == second["identity"]
    assert_success(client.post(_PATH, json={"requestId": "same-logout-id"}, headers=headers(second)))
    with factory() as db:
        records = db.scalars(select(LoginSession)).all()
        assert len(records) == 2
        assert all(record.revoked_at == _START for record in records)


@pytest.mark.parametrize("offset", [
    timedelta(hours=8), timedelta(hours=9), timedelta(seconds=-1),
])
def test_expired_or_not_yet_valid_session_can_be_permanently_logged_out(logged_in, offset):
    client, factory, clock_file, _, data = logged_in
    clock_file.write_text(format_time(_START + offset), encoding="utf-8")
    assert_success(client.post(_PATH, json={"requestId": "logout-1"}, headers=headers(data)))
    with factory() as db:
        assert db.scalars(select(LoginSession)).one().revoked_at == max(_START, _START + offset)
    # 仅改变本用例的隔离时钟，确认时间再次落入有效区间也不能恢复会话。
    clock_file.write_text(format_time(_START + timedelta(hours=1)), encoding="utf-8")
    assert_error(
        client.post("/api/v1/d/get_identity", json={}, headers=headers(data)),
        401, "UNAUTHENTICATED",
    )


def test_disabled_user_can_still_log_out(logged_in):
    client, factory, _, _, data = logged_in
    with factory.begin() as db:
        db.get(User, "u-student1").account_status = "DISABLED"
    assert_success(client.post(_PATH, json={"requestId": "logout-1"}, headers=headers(data)))
    with factory() as db:
        assert db.scalars(select(LoginSession)).one().revoked_at == _START


def test_unknown_well_formed_token_is_safe_and_changes_no_other_session(logged_in):
    client, factory, _, _, data = logged_in
    token = data["credential"]
    unknown = ("0" if token[0] != "0" else "1") + token[1:]
    auth = {"Authorization": f"Bearer {unknown}"}
    assert_success(client.post(_PATH, json={"requestId": "logout-1"}, headers=auth))
    assert_success(client.post(_PATH, json={"requestId": "logout-1"}, headers=auth))
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 1
        assert db.scalars(select(LoginSession)).one().revoked_at is None
    assert client.post("/api/v1/d/get_identity", json={}, headers=headers(data)).status_code == 200


def test_logout_needs_no_signing_key_and_new_login_requires_new_request_id(logged_in, monkeypatch):
    client, _, _, login_payload, old = logged_in
    monkeypatch.delenv("D_SESSION_SECRET")
    assert_success(client.post(_PATH, json={"requestId": "logout-1"}, headers=headers(old)))
    monkeypatch.setenv("D_SESSION_SECRET", "ab" * 32)
    assert_error(client.post("/api/v1/d/login", json=login_payload), 401, "AUTH_FAILED")
    response = client.post("/api/v1/d/login", json=login_payload | {"requestId": "new-login-id"})
    assert response.status_code == 200
    new = response.json()["data"]
    same_token = old["credential"] == new["credential"]
    assert not same_token
    assert client.post("/api/v1/d/get_identity", json={}, headers=headers(new)).status_code == 200


def test_database_failure_is_503_and_existing_session_remains_valid(logged_in, monkeypatch):
    client, _, _, _, data = logged_in

    def unavailable_database(module):
        raise SQLAlchemyTimeoutError("private-connection-detail")

    monkeypatch.setattr(logout_service, "session_factory", unavailable_database)
    response = client.post(_PATH, json={"requestId": "logout-1"}, headers=headers(data))
    assert_error(response, 503, "UNAVAILABLE")
    assert "private-connection-detail" not in response.text
    assert client.post("/api/v1/d/get_identity", json={}, headers=headers(data)).status_code == 200


def test_failed_transaction_rolls_back_revocation_then_same_request_can_retry(logged_in, monkeypatch):
    client, factory, _, _, data = logged_in

    class FailingFactory:
        @contextmanager
        def begin(self):
            with factory.begin() as db:
                yield db
                # 真实执行撤销 UPDATE 后，在事务提交前注入存储失败。
                raise OperationalError("private-query", {}, RuntimeError("private-detail"))

    with monkeypatch.context() as patch:
        patch.setattr(logout_service, "session_factory", lambda module: FailingFactory())
        response = client.post(_PATH, json={"requestId": "logout-1"}, headers=headers(data))
    assert_error(response, 503, "UNAVAILABLE")
    with factory() as db:
        assert db.scalars(select(LoginSession)).one().revoked_at is None
    assert client.post("/api/v1/d/get_identity", json={}, headers=headers(data)).status_code == 200
    assert_success(client.post(_PATH, json={"requestId": "logout-1"}, headers=headers(data)))
