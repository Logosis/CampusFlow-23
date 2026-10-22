"""D-S06：真实 PostgreSQL 数据/权限筛选、分页以及公开 HTTP 契约。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d import credentials, master, permissions, sessions
from backend.d import login as login_service
from backend.d import logout as logout_service
import backend.d.seed as seed_module
from backend.d.main import app
from backend.d.master_schemas import MASTER_INPUT, QueryMasterOutput
from backend.d.models import Base, LoginSession, Role, RoleGrant, Seat, Space, SystemGrant, SystemSubject, User
from backend.shared.clock import format_time
from backend.shared.errors import ApiError


PATH = "/api/v1/d/query_master"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
OUTPUT = TypeAdapter(QueryMasterOutput)


def query(operation="LIST_SPACES", *, mode="SYSTEM", peer="m1", user="u-student1", **fields):
    return master.query_master(
        input={"operation": operation, **fields}, actor_mode=mode,
        source_module=peer, user_id=user if mode == "USER" else None,
    )


def ids(result, key):
    return [row[key] for row in result["items"]]


def denied(code, action):
    with pytest.raises(ApiError) as error:
        action()
    assert error.value.code == code


def headers(mode="SYSTEM", peer="m1", token=None):
    result = {"X-Service-Credential": f"test-service-{peer}", "X-Actor-Mode": mode}
    if token is not None:
        result["Authorization"] = f"Bearer {token}"
    return result


def configure_client(monkeypatch):
    monkeypatch.setattr(app.state, "settings", replace(
        app.state.settings,
        credentials={peer: f"test-service-{peer}" for peer in ("d", "m1", "m2", "m3")},
    ))


@pytest.fixture
def client(monkeypatch):
    def unexpected(module):
        raise AssertionError("Rejected input must not reach database access")
    for service in (master, permissions, sessions, login_service, logout_service):
        monkeypatch.setattr(service, "session_factory", unexpected)
    configure_client(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value


@pytest.fixture
def master_db(isolated_db, monkeypatch, tmp_path):
    seed_module.seed()
    def factory(module):
        assert module == "d"
        return isolated_db
    for service in (master, permissions, sessions, login_service, logout_service):
        monkeypatch.setattr(service, "session_factory", factory)
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.setenv("D_SESSION_SECRET", "cd" * 32)
    clock = tmp_path / "clock.txt"
    clock.write_text(format_time(START), encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_CLOCK_FILE", str(clock))
    return isolated_db


@pytest.fixture
def db_client(master_db, monkeypatch):
    configure_client(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value


def restrict_user(factory, scopes):
    with factory.begin() as db:
        db.execute(delete(RoleGrant).where(RoleGrant.role_id == "r-student"))
        for kind, identifier in scopes:
            db.add(RoleGrant(role_id="r-student", permission_code="d.master.read", scope_type=kind, scope_id=identifier))


def login(client, fixture_data, account="student1"):
    result = client.post("/api/v1/d/login", json={
        "account": account, "password": fixture_data["password"], "requestId": f"master-login-{account}",
    })
    assert result.status_code == 200
    return result.json()["data"]["credential"]


def assert_http_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"ok", "data", "error"}
    assert body["ok"] is False and body["data"] is None
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code


@pytest.mark.parametrize("payload", [
    {}, {"operation": "unknown"}, {"operation": None},
    {"operation": "GET_SPACE"}, {"operation": "GET_SEAT"},
    {"operation": "GET_SPACE", "spaceId": " "},
    {"operation": "GET_SEAT", "seatId": 1},
    {"operation": "GET_SEAT", "seatId": "s", "limit": 1},
    {"operation": "GET_SPACE", "spaceId": "s", "enabled": True},
    {"operation": "LIST_DEPARTMENTS", "departmentId": "d"},
    {"operation": "LIST_DEPARTMENTS", "includeUnregistered": True},
    {"operation": "LIST_SPACES", "spaceId": "s"},
    {"operation": "LIST_SPACES", "socketTag": "FIXED"},
    {"operation": "LIST_SEATS", "ownerKind": "SCHOOL"},
    {"operation": "LIST_SEATS", "isStudyRoom": True},
    {"operation": "LIST_SEATS", "enabled": "false"},
    {"operation": "LIST_SPACES", "isStudyRoom": 1},
    {"operation": "LIST_SEATS", "limit": True},
    {"operation": "LIST_SEATS", "limit": "1"},
    {"operation": "LIST_SEATS", "limit": 0},
    {"operation": "LIST_SEATS", "limit": 101},
    {"operation": "LIST_SEATS", "limit": 1.5},
    {"operation": "LIST_SEATS", "cursor": ""},
    {"operation": "LIST_SEATS", "cursor": "x" * 8193},
    {"operation": "LIST_SEATS", "socketTag": "NONE"},
    {"operation": "LIST_SPACES", "ownerKind": "DEPT"},
    {"operation": "LIST_SPACES", "departmentId": " "},
    {"operation": "LIST_SEATS", "userId": "u-admin"},
    {"operation": "LIST_SEATS", "sourceModule": "m1"},
    {"operation": "LIST_SEATS", "grants": []},
    *[{"operation": "LIST_SEATS", field: None} for field in
      ("spaceId", "departmentId", "enabled", "socketTag", "includeUnregistered", "limit", "cursor")],
])
def test_input_rejects_invalid_or_wrong_operation_fields(payload):
    with pytest.raises(ValidationError):
        MASTER_INPUT.validate_python(payload)


@pytest.mark.parametrize("operation,fields", [
    ("LIST_DEPARTMENTS", {}), ("LIST_SPACES", {}), ("LIST_SEATS", {}),
    ("GET_SPACE", {"spaceId": "sp-school"}), ("GET_SEAT", {"seatId": "seat-school-1"}),
])
def test_all_operations_match_fixture_and_full_output(master_db, fixture_data, operation, fields):
    result = query(operation, **fields)
    assert OUTPUT.validate_python(result).model_dump() == result
    if operation == "LIST_DEPARTMENTS":
        assert result == {"items": sorted(fixture_data["departments"], key=lambda x: x["departmentId"]), "nextCursor": None}
    elif operation == "LIST_SPACES":
        assert result == {"items": sorted(fixture_data["spaces"], key=lambda x: x["spaceId"]), "nextCursor": None}
    elif operation == "LIST_SEATS":
        assert result == {"items": sorted(fixture_data["seats"], key=lambda x: x["seatId"]), "nextCursor": None}
    else:
        data_key, id_key = ("spaces", "spaceId") if operation == "GET_SPACE" else ("seats", "seatId")
        assert result == next(row for row in fixture_data[data_key] if row[id_key] == fields[id_key])


@pytest.mark.parametrize("scopes,departments,spaces,seats", [
    ([("GLOBAL", None)], ["dep-cs", "dep-ee"], ["sp-cs", "sp-school"], ["seat-cs-1", "seat-school-1", "seat-school-2"]),
    ([("DEPARTMENT", "dep-cs")], ["dep-cs"], ["sp-cs"], ["seat-cs-1"]),
    ([("SPACE", "sp-school")], [], ["sp-school"], ["seat-school-1", "seat-school-2"]),
    ([("DEPARTMENT", "dep-cs"), ("SPACE", "sp-cs")], ["dep-cs"], ["sp-cs"], ["seat-cs-1"]),
])
def test_scope_filters_do_not_expand_or_duplicate(master_db, scopes, departments, spaces, seats):
    restrict_user(master_db, scopes)
    assert ids(query("LIST_DEPARTMENTS", mode="USER"), "departmentId") == departments
    assert ids(query(mode="USER"), "spaceId") == spaces
    assert ids(query("LIST_SEATS", mode="USER"), "seatId") == seats


def test_explicit_filters_intersect_scope_and_details_reject(master_db):
    restrict_user(master_db, [("SPACE", "sp-school")])
    assert query(mode="USER", departmentId="dep-cs")["items"] == []
    assert query("LIST_SEATS", mode="USER", spaceId="sp-cs")["items"] == []
    denied("FORBIDDEN", lambda: query("GET_SPACE", mode="USER", spaceId="sp-cs"))
    denied("FORBIDDEN", lambda: query("GET_SEAT", mode="USER", seatId="seat-cs-1", includeUnregistered=True))
    assert query("GET_SEAT", mode="USER", seatId="seat-school-1")["spaceId"] == "sp-school"


def test_filters_and_disabled_records(master_db):
    with master_db.begin() as db:
        db.get(Space, "sp-school").enabled = False
        db.get(Space, "sp-cs").is_study_room = False
        db.get(Seat, "seat-school-1").enabled = False
        db.get(Seat, "seat-cs-1").socket_tags = ["RAIL", "FIXED"]
    assert ids(query(enabled=False), "spaceId") == ["sp-school"]
    assert ids(query(enabled=True), "spaceId") == ["sp-cs"]
    assert ids(query(isStudyRoom=False), "spaceId") == ["sp-cs"]
    assert ids(query(isStudyRoom=True), "spaceId") == ["sp-school"]
    assert ids(query(ownerKind="SCHOOL"), "spaceId") == ["sp-school"]
    assert ids(query(departmentId="dep-cs", ownerKind="DEPARTMENT"), "spaceId") == ["sp-cs"]
    assert query(ownerKind="SCHOOL", departmentId="dep-cs")["items"] == []
    assert ids(query("LIST_SEATS", enabled=False), "seatId") == ["seat-school-1"]
    assert ids(query("LIST_SEATS", enabled=True), "seatId") == ["seat-cs-1", "seat-school-2"]
    assert ids(query("LIST_SEATS", socketTag="FIXED"), "seatId") == ["seat-cs-1", "seat-school-1"]
    assert ids(query("LIST_SEATS", socketTag="RAIL"), "seatId") == ["seat-cs-1", "seat-school-2"]
    assert ids(query("LIST_SEATS", departmentId="dep-cs"), "seatId") == ["seat-cs-1"]
    assert ids(query("LIST_SEATS", spaceId="sp-school", socketTag="RAIL", enabled=True), "seatId") == ["seat-school-2"]
    assert query("LIST_SEATS", spaceId="sp-school", departmentId="dep-cs")["items"] == []
    assert query("GET_SPACE", spaceId="sp-school")["enabled"] is False
    assert query("GET_SEAT", seatId="seat-cs-1")["socketTags"] == ["FIXED", "RAIL"]


def test_unregistered_seats_and_parent_space_require_explicit_inclusion(master_db):
    with master_db.begin() as db:
        db.get(Space, "sp-school").registered = False
        db.get(Seat, "seat-cs-1").registered = False
    assert ids(query(), "spaceId") == ["sp-cs"]
    assert query("LIST_SEATS")["items"] == []
    assert len(query(includeUnregistered=True)["items"]) == 2
    assert len(query("LIST_SEATS", includeUnregistered=True)["items"]) == 3
    for operation, field, identifier in [
        ("GET_SPACE", "spaceId", "sp-school"),
        ("GET_SEAT", "seatId", "seat-cs-1"),
        ("GET_SEAT", "seatId", "seat-school-1"),
    ]:
        denied("NOT_FOUND", lambda: query(operation, **{field: identifier}))
        assert query(operation, includeUnregistered=True, **{field: identifier})[field] == identifier


@pytest.mark.parametrize("operation,key,expected", [
    ("LIST_DEPARTMENTS", "departmentId", ["dep-cs", "dep-ee"]),
    ("LIST_SPACES", "spaceId", ["sp-cs", "sp-school"]),
    ("LIST_SEATS", "seatId", ["seat-cs-1", "seat-school-1", "seat-school-2"]),
])
def test_keyset_pages_are_complete_and_repeatable(master_db, operation, key, expected):
    collected, options = [], {}
    for _ in range(len(expected)):
        page = query(operation, limit=1, **options)
        assert page == query(operation, limit=1, **options)
        assert len(page["items"]) == 1
        collected.extend(ids(page, key))
        if page["nextCursor"] is None:
            break
        options = {"cursor": page["nextCursor"]}
    assert collected == expected
    assert page["nextCursor"] is None
    assert query(operation, limit=100)["nextCursor"] is None


def test_invalid_cursor_and_changed_context_rejected(master_db):
    cursor = query("LIST_SEATS", limit=1)["nextCursor"]
    for fields in [
        {"limit": 2}, {"limit": 1, "socketTag": "RAIL"},
        {"limit": 1, "includeUnregistered": True}, {"limit": 1, "peer": "m2"},
        {"limit": 1, "mode": "USER"}, {"limit": 1, "operation": "LIST_SPACES"},
    ]:
        operation = fields.pop("operation", "LIST_SEATS")
        denied("INVALID_ARGUMENT", lambda: query(operation, cursor=cursor, **fields))
    denied("INVALID_ARGUMENT", lambda: query("LIST_SEATS", cursor="broken"))


def test_permissions_rechecked_on_next_page(master_db):
    cursor = query("LIST_SEATS", mode="USER", limit=1)["nextCursor"]
    restrict_user(master_db, [("SPACE", "sp-school")])
    denied("INVALID_ARGUMENT", lambda: query("LIST_SEATS", mode="USER", limit=1, cursor=cursor))
    assert len(query("LIST_SEATS", mode="USER")["items"]) == 2
    restrict_user(master_db, [])
    denied("FORBIDDEN", lambda: query("LIST_SEATS", mode="USER", limit=1, cursor=cursor))


def test_scope_applied_before_limit_and_key_survives_deleted_previous_row(master_db):
    restrict_user(master_db, [("SPACE", "sp-school")])
    first = query("LIST_SEATS", mode="USER", limit=1)
    assert ids(first, "seatId") == ["seat-school-1"]
    with master_db.begin() as db:
        db.delete(db.get(Seat, "seat-school-1"))
    second = query("LIST_SEATS", mode="USER", limit=1, cursor=first["nextCursor"])
    assert ids(second, "seatId") == ["seat-school-2"]
    assert second["nextCursor"] is None


@pytest.mark.parametrize("operation,fields", [
    ("GET_SPACE", {"spaceId": "missing"}), ("GET_SEAT", {"seatId": "missing"}),
    ("LIST_SPACES", {"departmentId": "missing"}),
    ("LIST_SEATS", {"departmentId": "missing"}), ("LIST_SEATS", {"spaceId": "missing"}),
])
def test_unknown_identifiers_are_not_empty_success(master_db, operation, fields):
    denied("NOT_FOUND", lambda: query(operation, **fields))


def test_system_scopes_and_disabled_subject(master_db):
    with master_db.begin() as db:
        db.execute(delete(SystemGrant).where(SystemGrant.module_id == "m1", SystemGrant.permission_code == "d.master.read"))
        db.add(SystemGrant(module_id="m1", permission_code="d.master.read", scope_type="DEPARTMENT", scope_id="dep-cs"))
    assert ids(query("LIST_SEATS"), "seatId") == ["seat-cs-1"]
    with master_db.begin() as db:
        db.get(SystemSubject, "m1").status = "DISABLED"
    denied("FORBIDDEN", lambda: query())
    assert len(query(peer="m2")["items"]) == 2


def test_queries_do_not_change_database(master_db):
    def snapshot():
        with master_db() as db:
            return {table.name: db.execute(select(table).order_by(*table.primary_key.columns)).all()
                    for table in Base.metadata.sorted_tables}
    before = snapshot()
    query(); query("LIST_DEPARTMENTS"); query("LIST_SEATS", limit=1)
    query("GET_SPACE", spaceId="sp-cs"); query("GET_SEAT", seatId="seat-cs-1")
    # 比较布尔值，失败时也不把密码哈希或会话记录打印出来。
    assert bool(before == snapshot())


@pytest.mark.parametrize("exception", [OperationalError("private-sql", {}, RuntimeError("private-detail")), SQLAlchemyTimeoutError("private-detail")])
def test_storage_failure_is_not_empty_success(monkeypatch, exception):
    def unavailable(module):
        raise exception
    monkeypatch.setattr(master, "session_factory", unavailable)
    with pytest.raises(ApiError) as error:
        query()
    assert error.value.code == "UNAVAILABLE"
    assert "private" not in error.value.message


def test_health_and_openapi_register_master(client):
    missing = client.get("/health").json()["data"]["unfinishedFunctions"]
    assert not {"login", "get_identity", "logout", "check_permission", "query_master"}.intersection(missing)
    assert "manage_roles" not in missing and "maintain_master" in missing
    operation = client.get("/openapi.json").json()["paths"][PATH]["post"]
    assert "requestBody" in operation and "200" in operation["responses"]


@pytest.mark.parametrize("request_headers,status,code", [
    ({}, 403, "FORBIDDEN"),
    ({"X-Service-Credential": "forged"}, 401, "UNAUTHENTICATED"),
    ({"X-Service-Credential": "test-service-m1"}, 422, "INVALID_ARGUMENT"),
    (headers(peer="d"), 403, "FORBIDDEN"),
    (headers(mode="BAD"), 422, "INVALID_ARGUMENT"),
    (headers(mode="USER"), 401, "UNAUTHENTICATED"),
    (headers(mode="USER", token="short"), 401, "UNAUTHENTICATED"),
    (headers(token="a" * 64), 422, "INVALID_ARGUMENT"),
])
def test_http_source_rules(client, request_headers, status, code):
    assert_http_error(client.post(PATH, json={"operation": "LIST_SPACES"}, headers=request_headers), status, code)


def test_http_rejects_injected_identity_without_accessing_database(client):
    assert_http_error(client.post(PATH, json={"operation": "LIST_SPACES", "userId": "u-admin"}, headers=headers()), 422, "INVALID_ARGUMENT")


def test_http_full_outputs_and_validation(db_client, fixture_data):
    for operation, fields in [
        ("LIST_DEPARTMENTS", {}), ("LIST_SPACES", {}), ("LIST_SEATS", {"limit": 1}),
        ("GET_SPACE", {"spaceId": "sp-school"}), ("GET_SEAT", {"seatId": "seat-school-1"}),
    ]:
        response = db_client.post(PATH, json={"operation": operation, **fields}, headers=headers())
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"ok", "data", "error"} and body["ok"] is True and body["error"] is None
        assert body["data"] == query(operation, **fields)
    for payload, status, code in [
        ({"operation": "GET_SEAT", "seatId": "missing"}, 404, "NOT_FOUND"),
        ({"operation": "LIST_SEATS", "cursor": "bad"}, 422, "INVALID_ARGUMENT"),
        ({"operation": "LIST_SEATS", "enabled": "false"}, 422, "INVALID_ARGUMENT"),
    ]:
        assert_http_error(db_client.post(PATH, json=payload, headers=headers()), status, code)


def test_http_user_scope_and_system_scope_separate_for_all_peers(db_client, fixture_data):
    token = login(db_client, fixture_data, "screen")
    for peer in ("m1", "m2", "m3"):
        user_headers = headers(mode="USER", peer=peer, token=token)
        response = db_client.post(PATH, json={"operation": "LIST_SEATS"}, headers=user_headers)
        assert response.status_code == 200
        assert ids(response.json()["data"], "seatId") == ["seat-school-1", "seat-school-2"]
        assert_http_error(db_client.post(PATH, json={"operation": "GET_SPACE", "spaceId": "sp-cs"}, headers=user_headers), 403, "FORBIDDEN")
        system = db_client.post(PATH, json={"operation": "LIST_SPACES"}, headers=headers(peer=peer))
        assert system.status_code == 200 and len(system.json()["data"]["items"]) == 2
        assert token not in response.text


@pytest.mark.parametrize("state", ["expired", "revoked", "disabled"])
def test_http_invalid_user_cannot_use_system_grants(master_db, db_client, fixture_data, state):
    token = login(db_client, fixture_data)
    with master_db.begin() as db:
        session = db.scalars(select(LoginSession)).one()
        if state == "expired":
            session.created_at = START - timedelta(hours=8)
            session.expires_at = START
        elif state == "revoked":
            session.revoked_at = START
        else:
            db.get(User, "u-student1").account_status = "DISABLED"
    assert_http_error(db_client.post(PATH, json={"operation": "LIST_SPACES"}, headers=headers(mode="USER", token=token)), 401, "UNAUTHENTICATED")


def test_http_role_revocation_is_immediate(master_db, db_client, fixture_data):
    token = login(db_client, fixture_data)
    request_headers = headers(mode="USER", token=token)
    assert db_client.post(PATH, json={"operation": "LIST_SPACES"}, headers=request_headers).status_code == 200
    with master_db.begin() as db:
        db.get(Role, "r-student").status = "DISABLED"
    assert_http_error(db_client.post(PATH, json={"operation": "LIST_SPACES"}, headers=request_headers), 403, "FORBIDDEN")


def test_http_database_failure_returns_503(client, monkeypatch):
    def unavailable(module):
        raise OperationalError("private-sql", {}, RuntimeError("private-detail"))
    monkeypatch.setattr(master, "session_factory", unavailable)
    response = client.post(PATH, json={"operation": "LIST_SPACES"}, headers=headers())
    assert_http_error(response, 503, "UNAVAILABLE")
    assert "private" not in response.text
