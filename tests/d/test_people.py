"""D-S08：实际人员和授权数据、按编号核验、候选分页及 HTTP 契约。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d import credentials, people, permissions, sessions
from backend.d import login as login_service
from backend.d import logout as logout_service
import backend.d.seed as seed_module
from backend.d.main import app
from backend.d.models import Base, LoginSession, Permission, Role, RoleGrant, Space, SystemGrant, SystemSubject, User, UserRole
from backend.d.people_schemas import PEOPLE_INPUT, QueryPeopleOutput
from backend.shared.clock import format_time
from backend.shared.errors import ApiError


PATH = "/api/v1/d/query_people"
GLOBAL = {"type": "GLOBAL", "id": None}
DEP_CS = {"type": "DEPARTMENT", "id": "dep-cs"}
DEP_EE = {"type": "DEPARTMENT", "id": "dep-ee"}
SPACE_CS = {"type": "SPACE", "id": "sp-cs"}
SCHOOL = {"type": "SPACE", "id": "sp-school"}
APPROVE = "m2.approval.approve"
REPAIR = "m3.repair.manage"
ACCEPT = "m3.acceptance.manage"
ESCALATE = "m3.escalation.receive"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
OUTPUT = TypeAdapter(QueryPeopleOutput)


def payload(**fields):
    return {"mode": "CANDIDATES", "purpose": "DISPATCH", "permissionCodes": [REPAIR], "targetScope": SCHOOL, **fields}


def query(*, peer="m3", actor="SYSTEM", user="u-dispatcher", **fields):
    return people.query_people(input=payload(**fields), source_module=peer, actor_mode=actor,
                               user_id=user if actor == "USER" else None)


def ids(result):
    return [person["userId"] for person in result["items"]]


def denied(code, action):
    with pytest.raises(ApiError) as error:
        action()
    assert error.value.code == code


def headers(peer="m3", actor="SYSTEM", token=None):
    result = {"X-Service-Credential": f"test-service-{peer}", "X-Actor-Mode": actor}
    if token is not None:
        result["Authorization"] = f"Bearer {token}"
    return result


def configure_client(monkeypatch):
    monkeypatch.setattr(app.state, "settings", replace(app.state.settings,
        credentials={peer: f"test-service-{peer}" for peer in ("d", "m1", "m2", "m3")}))


@pytest.fixture
def client(monkeypatch):
    def unexpected(module):
        raise AssertionError("Rejected requests must not access database")
    for service in (people, permissions, sessions, login_service, logout_service):
        monkeypatch.setattr(service, "session_factory", unexpected)
    configure_client(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value


@pytest.fixture
def people_db(isolated_db, monkeypatch, tmp_path):
    seed_module.seed()
    def factory(module):
        assert module == "d"
        return isolated_db
    for service in (people, permissions, sessions, login_service, logout_service):
        monkeypatch.setattr(service, "session_factory", factory)
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.setenv("D_SESSION_SECRET", "ef" * 32)
    clock = tmp_path / "clock.txt"
    clock.write_text(format_time(START), encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_CLOCK_FILE", str(clock))
    return isolated_db


@pytest.fixture
def db_client(people_db, monkeypatch):
    configure_client(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value


def login(client, fixture_data, account="dispatcher"):
    response = client.post("/api/v1/d/login", json={
        "account": account, "password": fixture_data["password"], "requestId": f"people-login-{account}",
    })
    assert response.status_code == 200
    return response.json()["data"]["credential"]


def assert_http_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"ok", "data", "error"}
    assert body["ok"] is False and body["data"] is None
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code


def replace_grants(db, role_id, grants):
    db.execute(delete(RoleGrant).where(RoleGrant.role_id == role_id))
    for code, scope in grants:
        db.add(RoleGrant(role_id=role_id, permission_code=code, scope_type=scope["type"], scope_id=scope["id"]))


@pytest.mark.parametrize("fields", [
    {"mode": "BAD"}, {"mode": None}, {"purpose": "BAD"}, {"purpose": None},
    {"purpose": "DISPLAY", "permissionCodes": []},
    {"permissionCodes": []}, {"permissionCodes": None}, {"permissionCodes": REPAIR},
    {"permissionCodes": [1]}, {"permissionCodes": [""]}, {"permissionCodes": [" "]},
    {"targetScope": None}, {"targetScope": {}}, {"targetScope": {"type": "GLOBAL", "id": "sp-school"}},
    {"targetScope": {"type": "SPACE", "id": None}}, {"targetScope": {"type": "SPACE", "id": " "}},
    {"targetScope": {"type": "BAD", "id": "sp-school"}}, {"targetScope": {"type": "GLOBAL"}},
    {"targetScope": {"type": "SPACE", "id": "sp-school", "extra": True}},
    {"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "1"}, {"limit": 1.5}, {"limit": None},
    {"cursor": ""}, {"cursor": " "}, {"cursor": 1}, {"cursor": None},
    {"userIds": []}, {"userId": "u-admin"}, {"sourceModule": "m3"}, {"grants": []},
    {"mode": "BY_IDS"}, {"mode": "BY_IDS", "userIds": None},
    {"mode": "BY_IDS", "userIds": "u-admin"}, {"mode": "BY_IDS", "userIds": [1]},
    {"mode": "BY_IDS", "userIds": [" "]}, {"mode": "BY_IDS", "userIds": [""]},
    {"mode": "BY_IDS", "userIds": [], "limit": 1}, {"mode": "BY_IDS", "userIds": [], "cursor": "x"},
    {"mode": "BY_IDS", "userIds": [], "purpose": "DISPLAY", "permissionCodes": [REPAIR]},
])
def test_input_rejects_invalid_fields(fields):
    with pytest.raises(ValidationError):
        PEOPLE_INPUT.validate_python(payload(**fields))


@pytest.mark.parametrize("field", ["mode", "purpose", "permissionCodes", "targetScope"])
def test_required_fields_cannot_be_omitted(field):
    value = payload()
    value.pop(field)
    with pytest.raises(ValidationError):
        PEOPLE_INPUT.validate_python(value)


def test_defaults_and_order_preserved():
    assert PEOPLE_INPUT.validate_python(payload()).limit == 20
    value = PEOPLE_INPUT.validate_python(payload(mode="BY_IDS", userIds=["b", "a", "b"]))
    assert value.userIds == ["b", "a", "b"]
    assert PEOPLE_INPUT.validate_python(payload(mode="BY_IDS", purpose="DISPLAY", permissionCodes=[], userIds=[])).userIds == []


@pytest.mark.parametrize("peer", ["m1", "d", None, "forged"])
def test_internal_source_rules_before_database(monkeypatch, peer):
    monkeypatch.setattr(people, "session_factory", lambda _: pytest.fail("Unexpected database access"))
    denied("FORBIDDEN", lambda: query(peer=peer))


@pytest.mark.parametrize("exception", [OperationalError("private-sql", {}, RuntimeError("private-detail")), SQLAlchemyTimeoutError("private-detail")])
def test_storage_error_is_not_empty_success(monkeypatch, exception):
    def unavailable(module):
        raise exception
    monkeypatch.setattr(people, "session_factory", unavailable)
    with pytest.raises(ApiError) as error:
        query()
    assert error.value.code == "UNAVAILABLE" and "private" not in error.value.message


@pytest.mark.parametrize("purpose,code,expected", [
    ("APPROVAL", APPROVE, ["u-admin", "u-approver1", "u-approver2"]),
    ("DISPATCH", REPAIR, ["u-admin", "u-technician"]),
    ("ACCEPTANCE", ACCEPT, ["u-admin", "u-inspector"]),
    ("ESCALATION", ESCALATE, ["u-admin", "u-supervisor"]),
])
def test_candidates_use_real_current_grants(people_db, fixture_data, purpose, code, expected):
    result = query(purpose=purpose, permissionCodes=[code])
    assert ids(result) == expected and result["nextCursor"] is None
    assert OUTPUT.validate_python(result).model_dump() == result
    fixtures = {user["userId"]: user for user in fixture_data["users"]}
    for person in result["items"]:
        original = fixtures[person["userId"]]
        assert person == {"userId": original["userId"], "displayName": original["displayName"],
                          "departmentId": original["departmentId"], "accountStatus": "ACTIVE",
                          "eligible": True, "matchedPermissionCodes": [code]}


def test_by_ids_distinguishes_partial_none_and_missing(people_db):
    requested = ["u-technician", "u-student1", "u-admin", "u-inspector", "u-technician"]
    result = query(mode="BY_IDS", userIds=requested, permissionCodes=[REPAIR, ACCEPT])
    assert OUTPUT.validate_python(result).model_dump() == result
    assert [row["userId"] for row in result["people"]] == requested
    assert [(row["eligible"], row["matchedPermissionCodes"]) for row in result["people"]] == [
        (False, [REPAIR]), (False, []), (True, sorted([REPAIR, ACCEPT])), (False, [ACCEPT]), (False, [REPAIR]),
    ]
    denied("NOT_FOUND", lambda: query(mode="BY_IDS", userIds=["u-admin", "not-a-user"]))
    assert query(mode="BY_IDS", userIds=[]) == {"people": []}


def test_display_is_not_qualification_and_never_includes_contacts(people_db):
    with people_db.begin() as db:
        db.get(User, "u-technician").account_status = "DISABLED"
    result = query(mode="BY_IDS", purpose="DISPLAY", permissionCodes=[], userIds=["u-technician", "u-student1"])
    for row in result["people"]:
        assert set(row) == {"userId", "displayName", "departmentId", "accountStatus", "eligible", "matchedPermissionCodes"}
        assert row["eligible"] is None and row["matchedPermissionCodes"] == []
    assert result["people"][0]["accountStatus"] == "DISABLED"
    denied("NOT_FOUND", lambda: query(mode="BY_IDS", purpose="DISPLAY", permissionCodes=[], userIds=["unknown"]))


@pytest.mark.parametrize("scope,target,expected", [
    (GLOBAL, GLOBAL, True), (GLOBAL, DEP_CS, True), (GLOBAL, SCHOOL, True),
    (DEP_CS, GLOBAL, False), (DEP_CS, DEP_CS, True), (DEP_CS, DEP_EE, False),
    (DEP_CS, SPACE_CS, True), (DEP_CS, SCHOOL, False),
    (SPACE_CS, GLOBAL, False), (SPACE_CS, DEP_CS, False), (SPACE_CS, SPACE_CS, True),
    (SPACE_CS, SCHOOL, False), (SCHOOL, SCHOOL, True),
])
def test_qualification_scope_matches_s04(people_db, scope, target, expected):
    with people_db.begin() as db:
        replace_grants(db, "r-technician", [(REPAIR, scope)])
    row = query(mode="BY_IDS", userIds=["u-technician"], targetScope=target)["people"][0]
    assert row["eligible"] is expected
    assert row["matchedPermissionCodes"] == ([REPAIR] if expected else [])
    s04 = permissions.check_permission(permission_code=REPAIR, target_scope=target, actor_mode="USER",
                                       source_module="m3", user_id="u-technician")
    assert s04["allowed"] is expected
    assert ("u-technician" in ids(query(targetScope=target))) is expected


def test_candidate_department_is_not_an_implicit_grant_or_filter(people_db):
    with people_db.begin() as db:
        db.get(User, "u-technician").department_id = "dep-ee"
        replace_grants(db, "r-technician", [(REPAIR, DEP_CS)])
    assert "u-technician" in ids(query(targetScope=SPACE_CS))
    with people_db.begin() as db:
        replace_grants(db, "r-technician", [])
        db.get(User, "u-technician").department_id = "dep-cs"
    assert "u-technician" not in ids(query(targetScope=SPACE_CS))


def test_all_permissions_can_come_from_different_roles_without_duplicate_people(people_db):
    with people_db.begin() as db:
        db.add(Role(role_id="r-test-duplicate", name="Duplicate repair grant", status="ACTIVE", version=1))
        db.flush()
        db.add(UserRole(user_id="u-technician", role_id="r-inspector"))
        db.add(UserRole(user_id="u-technician", role_id="r-test-duplicate"))
        db.add(RoleGrant(role_id="r-test-duplicate", permission_code=REPAIR, scope_type="GLOBAL", scope_id=None))
    result = query(permissionCodes=[REPAIR, ACCEPT, REPAIR])
    assert ids(result) == ["u-admin", "u-technician"]
    assert all(row["matchedPermissionCodes"] == sorted([REPAIR, ACCEPT]) for row in result["items"])


@pytest.mark.parametrize("change", ["disabled_user", "disabled_role", "removed_role", "revoked_grant", "disallowed_scope", "dangling_scope"])
def test_disabled_or_revoked_qualification_is_immediate(people_db, change):
    assert "u-technician" in ids(query())
    with people_db.begin() as db:
        if change == "disabled_user":
            db.get(User, "u-technician").account_status = "DISABLED"
        elif change == "disabled_role":
            db.get(Role, "r-technician").status = "DISABLED"
        elif change == "removed_role":
            db.execute(delete(UserRole).where(UserRole.user_id == "u-technician"))
        elif change == "revoked_grant":
            replace_grants(db, "r-technician", [])
        elif change == "disallowed_scope":
            db.get(Permission, REPAIR).allowed_scope_types = ["SPACE"]
        else:
            replace_grants(db, "r-technician", [(REPAIR, {"type": "SPACE", "id": "missing-space"})])
    assert "u-technician" not in ids(query())
    row = query(mode="BY_IDS", userIds=["u-technician"])["people"][0]
    assert row["eligible"] is False and row["matchedPermissionCodes"] == []


def test_empty_candidate_list_is_valid_after_filtering(people_db):
    with people_db.begin() as db:
        db.execute(delete(RoleGrant).where(RoleGrant.permission_code == REPAIR))
    assert query() == {"items": [], "nextCursor": None}


def test_target_space_ownership_and_history_use_current_scope(people_db):
    with people_db.begin() as db:
        replace_grants(db, "r-technician", [(REPAIR, DEP_CS)])
    assert "u-technician" in ids(query(targetScope=SPACE_CS))
    with people_db.begin() as db:
        space = db.get(Space, "sp-cs")
        space.registered = False
        space.enabled = False
    assert "u-technician" in ids(query(targetScope=SPACE_CS))
    with people_db.begin() as db:
        db.get(Space, "sp-cs").department_id = "dep-ee"
    assert "u-technician" not in ids(query(targetScope=SPACE_CS))


@pytest.mark.parametrize("actor", ["USER", "SYSTEM"])
def test_caller_must_have_people_permission_for_whole_target(people_db, actor):
    with people_db.begin() as db:
        if actor == "USER":
            replace_grants(db, "r-dispatcher", [("d.people.read", DEP_CS)])
        else:
            db.execute(delete(SystemGrant).where(SystemGrant.module_id == "m3", SystemGrant.permission_code == "d.people.read"))
            db.add(SystemGrant(module_id="m3", permission_code="d.people.read", scope_type="DEPARTMENT", scope_id="dep-cs"))
    assert query(actor=actor, targetScope=SPACE_CS)["items"]
    denied("FORBIDDEN", lambda: query(actor=actor, targetScope=SCHOOL))
    denied("FORBIDDEN", lambda: query(actor=actor, targetScope=GLOBAL))
    denied("FORBIDDEN", lambda: query(actor=actor, targetScope=SCHOOL, mode="BY_IDS", purpose="DISPLAY", permissionCodes=[], userIds=[]))


@pytest.mark.parametrize("fields", [
    {"permissionCodes": ["missing.permission"]},
    {"targetScope": {"type": "SPACE", "id": "missing"}},
    {"targetScope": {"type": "DEPARTMENT", "id": "missing"}},
])
def test_unknown_permission_or_target_is_not_empty_success(people_db, fields):
    denied("NOT_FOUND", lambda: query(**fields))


def test_candidates_paginate_eligible_people_before_limit(people_db):
    requested = {"purpose": "APPROVAL", "permissionCodes": [APPROVE], "limit": 1}
    collected, continuation = [], {}
    for _ in range(3):
        page = query(**requested, **continuation)
        assert page == query(**requested, **continuation)
        collected.extend(ids(page))
        if page["nextCursor"] is None:
            break
        continuation = {"cursor": page["nextCursor"]}
    assert collected == ["u-admin", "u-approver1", "u-approver2"]
    assert page["nextCursor"] is None
    # 维修人员排在多个不合格用户之后，不能先分页全部用户再筛选。
    first = query(limit=1)
    second = query(limit=1, cursor=first["nextCursor"])
    assert ids(first) == ["u-admin"] and ids(second) == ["u-technician"]
    assert second["nextCursor"] is None


def test_cursor_binds_query_and_identity(people_db):
    first = query(permissionCodes=[REPAIR, REPAIR], limit=1)
    cursor = first["nextCursor"]
    assert ids(query(permissionCodes=[REPAIR], limit=1, cursor=cursor)) == ["u-technician"]
    for changes in [
        {"purpose": "ACCEPTANCE"}, {"permissionCodes": [ACCEPT]}, {"targetScope": SPACE_CS},
        {"limit": 2}, {"peer": "m2"}, {"actor": "USER"},
    ]:
        denied("INVALID_ARGUMENT", lambda: query(**{"limit": 1, "cursor": cursor, **changes}))
    denied("INVALID_ARGUMENT", lambda: query(cursor="invalid"))


def test_caller_revocation_and_candidate_changes_between_pages(people_db):
    first = query(actor="USER", limit=1)
    with people_db.begin() as db:
        db.get(User, "u-technician").account_status = "DISABLED"
    assert query(actor="USER", limit=1, cursor=first["nextCursor"]) == {"items": [], "nextCursor": None}
    with people_db.begin() as db:
        replace_grants(db, "r-dispatcher", [("d.people.read", SCHOOL)])
    denied("INVALID_ARGUMENT", lambda: query(actor="USER", limit=1, cursor=first["nextCursor"]))
    with people_db.begin() as db:
        replace_grants(db, "r-dispatcher", [])
    denied("FORBIDDEN", lambda: query(actor="USER", limit=1, cursor=first["nextCursor"]))


def test_system_subject_disabled_or_missing_permission(people_db):
    with people_db.begin() as db:
        db.get(SystemSubject, "m3").status = "DISABLED"
    denied("FORBIDDEN", lambda: query())
    assert query(peer="m2")["items"]
    with people_db.begin() as db:
        db.execute(delete(SystemGrant).where(SystemGrant.module_id == "m2", SystemGrant.permission_code == "d.people.read"))
    denied("FORBIDDEN", lambda: query(peer="m2"))


def test_queries_do_not_write_data(people_db):
    def snapshot():
        with people_db() as db:
            return {table.name: db.execute(select(table).order_by(*table.primary_key.columns)).all()
                    for table in Base.metadata.sorted_tables}
    before = snapshot()
    query(limit=1)
    query(mode="BY_IDS", userIds=["u-technician", "u-student1"])
    query(mode="BY_IDS", purpose="DISPLAY", permissionCodes=[], userIds=["u-admin"])
    assert bool(before == snapshot())


def test_health_and_openapi_register_people(client):
    missing = client.get("/health").json()["data"]["unfinishedFunctions"]
    assert not {"login", "get_identity", "logout", "check_permission", "query_master", "query_people"}.intersection(missing)
    assert missing == []
    operation = client.get("/openapi.json").json()["paths"][PATH]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body["discriminator"]["propertyName"] == "mode" and len(body["oneOf"]) == 2
    assert "200" in operation["responses"]


@pytest.mark.parametrize("request_headers,status,code", [
    ({}, 403, "FORBIDDEN"),
    ({"X-Service-Credential": "forged"}, 401, "UNAUTHENTICATED"),
    ({"X-Service-Credential": "test-service-m3"}, 422, "INVALID_ARGUMENT"),
    (headers(peer="m1"), 403, "FORBIDDEN"), (headers(peer="d"), 403, "FORBIDDEN"),
    (headers(actor="BAD"), 422, "INVALID_ARGUMENT"),
    (headers(actor="USER"), 401, "UNAUTHENTICATED"),
    (headers(actor="USER", token="short"), 401, "UNAUTHENTICATED"),
    (headers(token="a" * 64), 422, "INVALID_ARGUMENT"),
])
def test_http_source_rules(client, request_headers, status, code):
    assert_http_error(client.post(PATH, json=payload(), headers=request_headers), status, code)


@pytest.mark.parametrize("fields", [
    {"userId": "u-admin"}, {"sourceModule": "m2"}, {"permissionCodes": []},
    {"mode": "BY_IDS"}, {"purpose": "DISPLAY", "permissionCodes": []},
])
def test_http_invalid_input_before_business_access(client, fields):
    assert_http_error(client.post(PATH, json=payload(**fields), headers=headers()), 422, "INVALID_ARGUMENT")


def test_http_full_output_user_and_system(db_client, fixture_data):
    token = login(db_client, fixture_data)
    for peer in ("m2", "m3"):
        for request_headers in (headers(peer=peer), headers(peer=peer, actor="USER", token=token)):
            for fields in ({}, {"mode": "BY_IDS", "userIds": ["u-technician", "u-student1"]},
                           {"mode": "BY_IDS", "purpose": "DISPLAY", "permissionCodes": [], "userIds": ["u-student1"]}):
                response = db_client.post(PATH, json=payload(**fields), headers=request_headers)
                assert response.status_code == 200
                assert response.json() == {"ok": True, "data": query(peer=peer, **fields), "error": None}
                assert token not in response.text and "password" not in response.text and "email" not in response.text


def test_http_business_errors_and_user_permission_are_distinct(db_client, fixture_data):
    for fields in ({"permissionCodes": ["missing.permission"]}, {"mode": "BY_IDS", "userIds": ["missing"]},
                   {"targetScope": {"type": "SPACE", "id": "missing"}}):
        assert_http_error(db_client.post(PATH, json=payload(**fields), headers=headers()), 404, "NOT_FOUND")
    assert_http_error(db_client.post(PATH, json=payload(cursor="bad"), headers=headers()), 422, "INVALID_ARGUMENT")
    token = login(db_client, fixture_data, "student1")
    assert_http_error(db_client.post(PATH, json=payload(), headers=headers(actor="USER", token=token)), 403, "FORBIDDEN")
    assert db_client.post(PATH, json=payload(), headers=headers()).status_code == 200


@pytest.mark.parametrize("state", ["expired", "revoked", "disabled"])
def test_http_invalid_user_never_falls_back_to_system(people_db, db_client, fixture_data, state):
    token = login(db_client, fixture_data)
    with people_db.begin() as db:
        session = db.scalars(select(LoginSession)).one()
        if state == "expired":
            session.created_at = START - timedelta(hours=8)
            session.expires_at = START
        elif state == "revoked":
            session.revoked_at = START
        else:
            db.get(User, "u-dispatcher").account_status = "DISABLED"
    assert_http_error(db_client.post(PATH, json=payload(), headers=headers(actor="USER", token=token)), 401, "UNAUTHENTICATED")
    assert db_client.post(PATH, json=payload(), headers=headers()).status_code == 200


def test_http_current_user_grants_are_rechecked(people_db, db_client, fixture_data):
    token = login(db_client, fixture_data)
    request_headers = headers(actor="USER", token=token)
    assert db_client.post(PATH, json=payload(), headers=request_headers).status_code == 200
    with people_db.begin() as db:
        db.get(Role, "r-dispatcher").status = "DISABLED"
    assert_http_error(db_client.post(PATH, json=payload(), headers=request_headers), 403, "FORBIDDEN")


def test_http_database_failure_returns_503(client, monkeypatch):
    def unavailable(module):
        raise OperationalError("private-sql", {}, RuntimeError("private-detail"))
    monkeypatch.setattr(people, "session_factory", unavailable)
    response = client.post(PATH, json=payload(), headers=headers())
    assert_http_error(response, 503, "UNAVAILABLE")
    assert "private" not in response.text
