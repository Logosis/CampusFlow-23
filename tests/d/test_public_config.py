"""D-S12：真实隔离 PostgreSQL、迁移、幂等/版本/回滚与 HTTP 契约。"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import re

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from backend.d import credentials, public_config, sessions
from backend.d import login as login_service
import backend.d.seed as seed_module
from backend.d.main import app
from backend.d.models import Base, PublicConfig, PublicConfigRequest, Role, RoleGrant, User, UserRole
from backend.d.public_config_schemas import CONFIG_INPUT, PublicConfigOutput
from backend.shared.clock import format_time
from backend.shared.errors import ApiError
from backend.shared.settings import ROOT


PATH = "/api/v1/d/manage_public_config"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
DEFAULT = {"authMethod": "PASSWORD", "notificationChannel": "EMAIL", "notificationRetryLimit": 2, "version": 1}


def payload(**fields):
    return {"operation": "UPDATE", "authMethod": "PASSWORD", "notificationChannel": "EMAIL",
            "notificationRetryLimit": 3, "expectedVersion": 1, "requestId": "config-request-1", **fields}


def perform(data=None, *, user="u-admin", peer="m1", actor="USER"):
    return public_config.manage_public_config(input={"operation": "GET"} if data is None else data,
                                             actor_mode=actor, source_module=peer, user_id=user)


def denied(code, action):
    with pytest.raises(ApiError) as caught:
        action()
    assert caught.value.code == code


def snapshot(factory, excluded=()):
    with factory() as db:
        return {table.name: db.execute(select(table).order_by(*table.primary_key.columns)).all()
                for table in Base.metadata.sorted_tables if table.name not in excluded}


@pytest.fixture
def config_db(isolated_db, monkeypatch, tmp_path):
    seed_module.seed()
    def factory(module):
        assert module == "d"
        return isolated_db
    for service in (public_config, sessions, login_service):
        monkeypatch.setattr(service, "session_factory", factory)
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.setenv("D_SESSION_SECRET", "ad" * 32)
    clock = tmp_path / "clock.txt"
    clock.write_text(format_time(START), encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_CLOCK_FILE", str(clock))
    return isolated_db


@pytest.fixture
def db_client(config_db, monkeypatch):
    monkeypatch.setattr(app.state, "settings", replace(app.state.settings,
        credentials={peer: f"test-service-{peer}" for peer in ("d", "m1", "m2", "m3")}))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def headers(token=None, peer="m1", actor="USER"):
    result = {"X-Service-Credential": f"test-service-{peer}", "X-Actor-Mode": actor}
    if token is not None:
        result["Authorization"] = f"Bearer {token}"
    return result


def login(client, fixture_data, account="admin"):
    response = client.post("/api/v1/d/login", json={"account": account, "password": fixture_data["password"],
                                                              "requestId": f"config-login-{account}"})
    assert response.status_code == 200
    return response.json()["data"]["credential"]


def http_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert body["ok"] is False and body["data"] is None and body["error"]["code"] == code
    assert set(body) == {"ok", "data", "error"}


@pytest.mark.parametrize("data", [
    {}, {"operation": "get"}, {"operation": "UNKNOWN"}, {"operation": None}, [], None,
    {"operation": "GET", "requestId": "x"}, {"operation": "GET", "expectedVersion": 1},
    {"operation": "GET", "userId": "u-admin"}, {"operation": "GET", "notificationRetryLimit": 2},
    *[payload(notificationRetryLimit=v) for v in (-1, 6, True, "2", 2.0, None)],
    *[payload(expectedVersion=v) for v in (0, -1, True, "1", 1.0, None)],
    *[payload(requestId=v) for v in ("", " \t", None, 12)],
    payload(authMethod="SSO"), payload(authMethod=None), payload(notificationChannel="SMS"),
    payload(notificationChannel=None), payload(version=1), payload(userId="u-admin"),
    payload(sourceModule="M1"), payload(password="not-a-secret-test-value"),
])
def test_invalid_config_input(data):
    with pytest.raises(ValidationError):
        CONFIG_INPUT.validate_python(data)
    denied("INVALID_ARGUMENT", lambda: public_config.manage_public_config(
        input=data, source_module="m1", actor_mode="USER", user_id="u-admin"))


@pytest.mark.parametrize("field", ["authMethod", "notificationChannel", "notificationRetryLimit", "expectedVersion", "requestId"])
def test_update_requires_every_field(field):
    data = payload()
    del data[field]
    with pytest.raises(ValidationError):
        CONFIG_INPUT.validate_python(data)


@pytest.mark.parametrize("value", [0, 5])
def test_valid_boundary_input(value):
    assert CONFIG_INPUT.validate_python(payload(notificationRetryLimit=value)).notificationRetryLimit == value


@pytest.mark.parametrize("peer,actor,user,code", [
    (None, "USER", "u-admin", "FORBIDDEN"), ("m2", "USER", "u-admin", "FORBIDDEN"),
    ("m3", "USER", "u-admin", "FORBIDDEN"), ("m1", "SYSTEM", None, "FORBIDDEN"),
    ("m1", "USER", None, "UNAUTHENTICATED"), ("m1", "USER", " ", "UNAUTHENTICATED"),
])
def test_rejected_actor_does_not_open_database(monkeypatch, peer, actor, user, code):
    def unexpected(module):
        raise AssertionError("No database access expected")
    monkeypatch.setattr(public_config, "session_factory", unexpected)
    denied(code, lambda: perform(peer=peer, actor=actor, user=user))


@pytest.mark.parametrize("error", [OperationalError("private-sql", {}, RuntimeError("private-detail")),
                                 InterfaceError("private-sql", {}, RuntimeError("private-detail")),
                                 SQLAlchemyTimeoutError("private-detail")])
def test_storage_failure_is_unavailable(monkeypatch, error):
    def unavailable(module):
        raise error
    monkeypatch.setattr(public_config, "session_factory", unavailable)
    with pytest.raises(ApiError) as caught:
        perform(payload())
    assert caught.value.code == "UNAVAILABLE" and "private" not in caught.value.message


def test_seed_initial_config_and_repeat_preserves_changes(config_db, fixture_data):
    assert perform() == DEFAULT
    assert perform()["notificationRetryLimit"] == fixture_data["d"]["notificationRetryLimit"]
    perform(payload(notificationRetryLimit=5))
    before = snapshot(config_db)
    seed_module.seed()
    unchanged = snapshot(config_db) == before
    assert unchanged, "Existing rows must be preserved"
    assert perform()["notificationRetryLimit"] == 5 and perform()["version"] == 2


@pytest.mark.parametrize("changes", [
    {"notificationRetryLimit": -1}, {"notificationRetryLimit": 6}, {"notificationRetryLimit": True},
    {"notificationRetryLimit": "2"}, {"authMethod": "SSO"}, {"notificationChannel": "SMS"},
])
def test_invalid_seed_config_rolls_back_all_new_data(isolated_db, fixture_data, monkeypatch, tmp_path, changes):
    fixture_data["d"].update(changes)
    path = tmp_path / "tests/fixtures/bootstrap.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(fixture_data), encoding="utf-8")
    monkeypatch.setattr(seed_module, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="Invalid public config"):
        seed_module.seed()
    with isolated_db() as db:
        for table in Base.metadata.sorted_tables:
            assert db.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize("changes", [
    {"config_id": 2}, {"auth_method": "SSO"}, {"notification_channel": "SMS"},
    {"notification_retry_limit": -1}, {"notification_retry_limit": 6}, {"version": 0},
])
def test_database_enforces_config_constraints(isolated_db, changes):
    with pytest.raises(IntegrityError) as caught:
        with isolated_db.begin() as db:
            db.add(PublicConfig(**({"config_id": 1, "auth_method": "PASSWORD", "notification_channel": "EMAIL",
                                  "notification_retry_limit": 2, "version": 1} | changes)))
    assert caught.value.orig.sqlstate == "23514"


def test_migration_round_trip_preserves_previous_eleven_tables(config_db):
    excluded = {"public_config", "public_config_requests"}
    before = snapshot(config_db, excluded)
    module = ScriptDirectory(str(ROOT / "backend/d/migrations")).get_revision("d_b7e4a921c630").module
    with config_db.begin() as db:
        assert re.fullmatch(r"d_seed_test_[0-9a-f]{32}", db.scalar(text("SELECT current_schema()")))
        with Operations.context(MigrationContext.configure(db.connection())):
            module.downgrade()
            module.upgrade()
        assert db.get(PublicConfig, 1) is None
        assert db.scalar(select(func.count()).select_from(PublicConfigRequest)) == 0
    unchanged = snapshot(config_db, excluded) == before
    assert unchanged, "Existing rows must be preserved"
    seed_module.seed()
    unchanged = snapshot(config_db, excluded) == before
    assert unchanged, "Existing rows must be preserved"
    assert perform() == DEFAULT


def test_get_is_read_only_and_has_exact_public_fields(config_db):
    before = snapshot(config_db)
    result = perform()
    assert result == DEFAULT and PublicConfigOutput.model_validate(result).model_dump() == result
    unchanged = snapshot(config_db) == before
    assert unchanged, "Existing rows must be preserved"


@pytest.mark.parametrize("value", [0, 2, 5])
def test_update_persists_once_and_repeat_ignores_old_version(config_db, value):
    result = perform(payload(notificationRetryLimit=value))
    assert result == {**DEFAULT, "notificationRetryLimit": value, "version": 2}
    before = snapshot(config_db)
    assert perform(payload(notificationRetryLimit=value)) == result
    unchanged = snapshot(config_db) == before
    assert unchanged, "Existing rows must be preserved"
    assert perform() == result
    with config_db() as db:
        record = db.get(PublicConfigRequest, "config-request-1")
        assert record.input_data == {key: value for key, value in payload(notificationRetryLimit=value).items() if key != "requestId"}
        assert record.response_data == result and record.requested_by_user_id == "u-admin"
        assert record.created_at == START
        assert db.scalar(select(func.count()).select_from(PublicConfigRequest)) == 1


def test_old_success_returns_original_result_without_overwriting_later_update(config_db):
    original = perform(payload())
    current = perform(payload(requestId="next", expectedVersion=2, notificationRetryLimit=5))
    assert perform(payload()) == original
    assert perform() == current and current["version"] == 3


@pytest.mark.parametrize("changes", [{"notificationRetryLimit": 4}, {"expectedVersion": 2}])
def test_reused_request_with_changed_content_is_rejected(config_db, changes):
    perform(payload())
    before = snapshot(config_db)
    denied("REQUEST_ID_REUSED", lambda: perform(payload(**changes)))
    unchanged = snapshot(config_db) == before
    assert unchanged, "Existing rows must be preserved"


def test_stale_new_request_is_rejected_without_success_record(config_db):
    perform(payload())
    before = snapshot(config_db)
    denied("CONFLICT", lambda: perform(payload(requestId="stale")))
    unchanged = snapshot(config_db) == before
    assert unchanged, "Existing rows must be preserved"
    assert perform(payload(requestId="stale", expectedVersion=2))["version"] == 3


@pytest.mark.parametrize("operation,permission", [("GET", "d.config.read"), ("UPDATE", "d.config.manage")])
def test_operations_require_their_specific_global_permission(config_db, operation, permission):
    with config_db.begin() as db:
        db.execute(delete(RoleGrant).where(RoleGrant.role_id == "r-admin", RoleGrant.permission_code == permission))
    data = {"operation": "GET"} if operation == "GET" else payload()
    denied("FORBIDDEN", lambda: perform(data))
    if operation == "GET":
        assert perform(payload())["version"] == 2
    else:
        assert perform() == DEFAULT


@pytest.mark.parametrize("scope_type,scope_id", [("DEPARTMENT", "dep-cs"), ("SPACE", "sp-school")])
def test_local_scope_never_allows_global_configuration(config_db, scope_type, scope_id):
    with config_db.begin() as db:
        for grant in db.scalars(select(RoleGrant).where(RoleGrant.role_id == "r-admin",
                                                       RoleGrant.permission_code.in_(["d.config.read", "d.config.manage"]))):
            grant.scope_type, grant.scope_id = scope_type, scope_id
    denied("FORBIDDEN", perform)
    denied("FORBIDDEN", lambda: perform(payload()))


@pytest.mark.parametrize("change,code", [("user", "UNAUTHENTICATED"), ("role", "FORBIDDEN"), ("grant", "FORBIDDEN")])
def test_repeat_rechecks_current_authority(config_db, change, code):
    perform(payload())
    with config_db.begin() as db:
        if change == "user":
            db.get(User, "u-admin").account_status = "DISABLED"
        elif change == "role":
            db.get(Role, "r-admin").status = "DISABLED"
        else:
            db.execute(delete(RoleGrant).where(RoleGrant.role_id == "r-admin", RoleGrant.permission_code == "d.config.manage"))
    before = snapshot(config_db)
    denied(code, lambda: perform(payload()))
    unchanged = snapshot(config_db) == before
    assert unchanged, "Existing rows must be preserved"


def test_m1_users_share_the_same_request_namespace(config_db):
    with config_db.begin() as db:
        db.add(UserRole(user_id="u-student1", role_id="r-admin"))
    first = perform(payload())
    assert perform(payload(), user="u-student1") == first
    denied("REQUEST_ID_REUSED", lambda: perform(payload(notificationRetryLimit=4), user="u-student1"))
    with config_db() as db:
        assert db.scalar(select(func.count()).select_from(PublicConfigRequest)) == 1
        assert db.get(PublicConfigRequest, "config-request-1").requested_by_user_id == "u-admin"


def test_uninitialized_config_fails_instead_of_returning_defaults(config_db):
    with config_db.begin() as db:
        db.execute(delete(PublicConfig))
    denied("UNAVAILABLE", perform)
    denied("UNAVAILABLE", lambda: perform(payload()))
    denied("FORBIDDEN", lambda: perform(user="u-student1"))


def test_failure_before_commit_rolls_back_config_and_success_record(config_db, monkeypatch):
    class FailingSession(Session):
        pass
    def fail(session):
        raise OperationalError("commit", {}, RuntimeError("private-detail"))
    event.listen(FailingSession, "before_commit", fail)
    factory = sessionmaker(bind=config_db.kw["bind"], class_=FailingSession, expire_on_commit=False,
                           join_transaction_mode="create_savepoint")
    before = snapshot(config_db)
    monkeypatch.setattr(public_config, "session_factory", lambda module: factory)
    denied("UNAVAILABLE", lambda: perform(payload()))
    unchanged = snapshot(config_db) == before
    assert unchanged, "Existing rows must be preserved"
    monkeypatch.setattr(public_config, "session_factory", lambda module: config_db)
    assert perform(payload())["version"] == 2


def test_uncertain_response_after_commit_can_be_retried_without_reapplying(config_db, monkeypatch):
    class UncertainFactory:
        @contextmanager
        def begin(self):
            with config_db.begin() as db:
                yield db
            raise OperationalError("commit-response", {}, RuntimeError("private-detail"))
    monkeypatch.setattr(public_config, "session_factory", lambda module: UncertainFactory())
    denied("UNAVAILABLE", lambda: perform(payload()))
    monkeypatch.setattr(public_config, "session_factory", lambda module: config_db)
    assert perform(payload()) == {**DEFAULT, "notificationRetryLimit": 3, "version": 2}
    assert perform()["version"] == 2
    with config_db() as db:
        assert db.scalar(select(func.count()).select_from(PublicConfigRequest)) == 1


def test_http_health_and_full_success_conflict_and_idempotency(db_client, fixture_data):
    missing = db_client.get("/health").json()["data"]["unfinishedFunctions"]
    assert "manage_public_config" not in missing and "audit" in missing
    schema = db_client.get("/openapi.json").json()
    body_schema = schema["paths"][PATH]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema["discriminator"]["propertyName"] == "operation"
    token = login(db_client, fixture_data)
    def call(data):
        return db_client.post(PATH, json=data, headers=headers(token))
    assert call({"operation": "GET"}).json() == {"ok": True, "data": DEFAULT, "error": None}
    first = call(payload())
    assert first.status_code == 200 and first.json()["data"] == {**DEFAULT, "notificationRetryLimit": 3, "version": 2}
    assert call(payload()).json() == first.json()
    http_error(call(payload(notificationRetryLimit=4)), 409, "REQUEST_ID_REUSED")
    http_error(call(payload(requestId="stale-http")), 409, "CONFLICT")
    http_error(call(payload(notificationRetryLimit=True)), 422, "INVALID_ARGUMENT")
    http_error(call({"operation": "GET", "userId": "u-admin"}), 422, "INVALID_ARGUMENT")
    assert token not in first.text
    assert set(first.json()["data"]) == set(DEFAULT)


def test_http_peer_mode_and_user_authority(db_client, fixture_data):
    admin = login(db_client, fixture_data)
    student = login(db_client, fixture_data, "student1")
    for request_headers, status, code in [
        (headers(admin, peer="m2"), 403, "FORBIDDEN"), (headers(admin, peer="m3"), 403, "FORBIDDEN"),
        ({"Authorization": f"Bearer {admin}"}, 403, "FORBIDDEN"),
        (headers(actor="SYSTEM"), 403, "FORBIDDEN"), (headers(), 401, "UNAUTHENTICATED"),
        (headers("a" * 64), 401, "UNAUTHENTICATED"), (headers(student), 403, "FORBIDDEN"),
    ]:
        http_error(db_client.post(PATH, json={"operation": "GET"}, headers=request_headers), status, code)


def test_http_repeat_after_authority_revoked_is_rejected(config_db, db_client, fixture_data):
    token = login(db_client, fixture_data)
    assert db_client.post(PATH, json=payload(), headers=headers(token)).status_code == 200
    with config_db.begin() as db:
        db.get(Role, "r-admin").status = "DISABLED"
    http_error(db_client.post(PATH, json=payload(), headers=headers(token)), 403, "FORBIDDEN")


def test_http_storage_failure_has_no_private_details(db_client, fixture_data, monkeypatch):
    token = login(db_client, fixture_data)
    def fail(module):
        raise OperationalError("private-sql", {}, RuntimeError("private-detail"))
    monkeypatch.setattr(public_config, "session_factory", fail)
    response = db_client.post(PATH, json=payload(), headers=headers(token))
    http_error(response, 503, "UNAVAILABLE")
    assert "private" not in response.text and token not in response.text
