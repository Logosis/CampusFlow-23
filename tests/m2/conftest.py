"""M2 模块测试的共享夹具。

外部依赖（D 身份/权限/主数据/人员/通知/审计、M1 整室可用性与限制）按提供方契约模拟：
只替换外部调用，不绕过 M2 自己的权限、版本、状态与参数校验，也不模拟 M2 自身业务。
数据库使用独立 SQLite 文件，不触碰共享 PostgreSQL。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "tests/fixtures/bootstrap.json").read_text(encoding="utf-8"))

# 必须在导入 backend 之前设定：Settings.load 从环境变量读取
_DB_PATH = ROOT / ".m2_test.sqlite3"
os.environ["APP_ENV"] = "test"
os.environ["M2_DATABASE_URL"] = f"sqlite+pysqlite:///{_DB_PATH.as_posix()}"
os.environ.setdefault("SERVICE_CREDENTIAL_M2", "secret-m2")
os.environ.setdefault("D_BASE_URL", "http://d.test")
os.environ.setdefault("M1_BASE_URL", "http://m1.test")

from fastapi.testclient import TestClient  # noqa: E402

from backend.m2 import peers  # noqa: E402
from backend.m2.models import Base  # noqa: E402
from backend.shared.db import session_factory  # noqa: E402
from backend.shared.errors import ApiError  # noqa: E402

USERS = {u["userId"]: u for u in FIXTURE["users"]}
ROLES = {role["roleId"]: role for role in FIXTURE["roles"].values()}
ROLE_OF = {u["userId"]: u["roleIds"][0] for u in FIXTURE["users"]}
SPACES = {s["spaceId"]: s for s in FIXTURE["spaces"]}
TOKEN_OF = {u["account"]: f"token-{u['account']}" for u in FIXTURE["users"]}
USER_OF_TOKEN = {token: u["userId"] for account, token in TOKEN_OF.items() for u in FIXTURE["users"]
                 if u["account"] == account}


def identity_for(token: str) -> dict:
    user_id = USER_OF_TOKEN.get(token)
    if user_id is None:
        raise ApiError("UNAUTHENTICATED", "凭据无效")
    user = USERS[user_id]
    role = ROLES[ROLE_OF[user_id]]
    return {
        "userId": user_id,
        "displayName": user["displayName"],
        "departmentId": user["departmentId"],
        "accountStatus": user["accountStatus"],
        "roleIds": list(user["roleIds"]),
        "grants": [{"permissionCode": code, "scope": role["scope"]}
                   for code in role["permissionCodes"]],
    }


class FakeWorld:
    """可编程的 D/M1 替身，记录 M2 实际发出的请求。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.availability: dict[tuple[str, str, str], dict] = {}
        self.default_availability = {"canOccupy": True, "blockedBy": [], "reasons": []}
        self.limit_outcome: dict[str, object] = {}
        self.limit_default = "SUCCESS"
        self.people_override: dict[str, dict] = {}
        self.permission_override: dict[str, dict[str, bool]] = {}
        self.extra_permissions: dict[str, list[str]] = {}
        self.notification_error = False
        self.audit_error = False

    def availability_for(self, space_id: str, start: str, end: str) -> dict:
        return self.availability.get((space_id, start, end), self.default_availability)

    def block(self, space_id: str, start: str, end: str, *, blocked_by: list[str],
              reasons: list[str] | None = None) -> None:
        self.availability[(space_id, start, end)] = {
            "canOccupy": False, "blockedBy": list(blocked_by),
            "reasons": list(reasons or ["测试用途"]),
        }


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    fake = FakeWorld()

    async def check_permission(code, scope, actor):
        fake.calls.append(("d", "check_permission", {"code": code, "scope": scope,
                                                     "mode": actor.mode}))
        if actor.mode == "SYSTEM":
            grants = FIXTURE["systemGrants"]["m2"]["permissionCodes"]
            return {"subjectType": "SYSTEM", "subjectId": "m2", "allowed": code in grants,
                    "allowedScopes": [{"type": "GLOBAL", "id": None}], "reason": ""}
        user_id = USER_OF_TOKEN.get(actor.user_credential or "")
        if user_id is None:
            raise ApiError("UNAUTHENTICATED", "需要用户登录")
        override = fake.permission_override.get(user_id)
        if override is not None and code in override:
            allowed = override[code]
            return {"subjectType": "USER", "subjectId": user_id, "allowed": allowed,
                    "allowedScopes": [{"type": "GLOBAL", "id": None}], "reason": ""}
        role = ROLES[ROLE_OF[user_id]]
        allowed = code in role["permissionCodes"] or code in fake.extra_permissions.get(user_id, [])
        return {"subjectType": "USER", "subjectId": user_id, "allowed": allowed,
                "allowedScopes": [role["scope"]] if allowed else [],
                "reason": "" if allowed else "权限不足"}

    async def get_space(space_id, actor):
        fake.calls.append(("d", "query_master", {"operation": "GET_SPACE", "spaceId": space_id}))
        space = SPACES.get(space_id)
        if space is None:
            raise ApiError("NOT_FOUND", "空间不存在")
        return dict(space)

    async def list_spaces(filters, actor):
        fake.calls.append(("d", "query_master", {"operation": "LIST_SPACES", **filters}))
        items = list(SPACES.values())
        if filters.get("departmentId"):
            items = [s for s in items if s["departmentId"] == filters["departmentId"]]
        return {"items": [dict(s) for s in items], "nextCursor": None}

    async def people_by_ids(user_ids, scope, actor):
        fake.calls.append(("d", "query_people", {"userIds": list(user_ids), "targetScope": scope}))
        result = []
        for user_id in user_ids:
            user = USERS.get(user_id)
            if user is None:
                raise ApiError("NOT_FOUND", f"人员 {user_id} 不存在")
            override = fake.people_override.get(user_id) or {}
            role = ROLES[ROLE_OF[user_id]]
            eligible = "m2.approval.approve" in role["permissionCodes"]
            if "eligible" in override:
                eligible = bool(override["eligible"])
            result.append({
                "userId": user_id,
                "displayName": user["displayName"],
                "departmentId": user["departmentId"],
                "accountStatus": override.get("accountStatus", user["accountStatus"]),
                "eligible": eligible,
                "matchedPermissionCodes": ["m2.approval.approve"] if eligible else [],
            })
        return result

    async def room_availability(space_id, start_time, end_time, actor):
        fake.calls.append(("m1", "get_room_availability",
                           {"spaceId": space_id, "startTime": start_time, "endTime": end_time}))
        state = fake.availability_for(space_id, start_time, end_time)
        return {"spaceId": space_id, "startTime": start_time, "endTime": end_time,
                "canOccupy": state["canOccupy"], "blockedBy": list(state["blockedBy"]),
                "reasons": list(state["reasons"]), "checkedAt": "2026-01-01T00:00:00Z"}

    async def change_room_limit(payload):
        fake.calls.append(("m1", "change_room_limit", payload))
        outcome = fake.limit_outcome.get(payload["requestId"], fake.limit_default)
        if isinstance(outcome, Exception):
            raise outcome
        status = "SUCCESS" if outcome == "SUCCESS" else "REJECTED"
        return {"source": payload["source"], "businessId": payload["businessId"],
                "spaceId": payload["spaceId"], "maxProcessedVersion": payload["sourceVersion"],
                "currentLimit": {
                    "source": payload["source"], "businessId": payload["businessId"],
                    "spaceId": payload["spaceId"], "startTime": payload.get("startTime"),
                    "endTime": payload.get("endTime"),
                    "state": "ACTIVE" if payload["action"] == "OCCUPY" else "RELEASED",
                    "sourceVersion": payload["sourceVersion"]},
                "queriedRequestId": payload["requestId"], "requestStatus": status, "message": ""}

    async def room_limit(source, business_id, request_id):
        fake.calls.append(("m1", "get_room_limit",
                           {"source": source, "businessId": business_id, "requestId": request_id}))
        return {"source": source, "businessId": business_id, "spaceId": None,
                "maxProcessedVersion": None, "currentLimit": None,
                "queriedRequestId": request_id, "requestStatus": "UNKNOWN", "message": ""}

    async def send_notification(payload):
        fake.calls.append(("d", "send_notification", payload))
        if fake.notification_error:
            raise ApiError("UNAVAILABLE", "通知服务不可用")
        return {"notificationId": "n-" + payload["requestId"], "requestId": payload["requestId"],
                "sourceModule": payload["sourceModule"], "businessId": payload["businessId"],
                "spaceId": payload.get("spaceId"), "kind": payload["kind"], "channel": "EMAIL",
                "title": payload["title"], "body": payload["body"],
                "createdAt": "2026-01-01T00:00:00Z",
                "deliveries": [{"recipientUserId": rid, "status": "PENDING",
                                "attemptCount": 0, "lastAttemptAt": None, "reason": ""}
                               for rid in payload["recipientUserIds"]]}

    async def audit_append(payload):
        fake.calls.append(("d", "audit", payload))
        if fake.audit_error:
            raise ApiError("UNAVAILABLE", "审计服务不可用")
        from backend.d.audit_schemas import AuditAppendInput
        event = AuditAppendInput.model_validate({"operation": "APPEND", **payload})
        return {"auditId": "a-" + payload["requestId"], **event.model_dump(exclude={"operation"}), "verifiedBy": "M2"}

    async def identity_call(settings, target, function, payload, actor, **kwargs):
        fake.calls.append((target, "get_identity", {"mode": actor.mode}))
        return identity_for(actor.user_credential or "")

    monkeypatch.setattr(peers, "check_permission", check_permission)
    monkeypatch.setattr(peers, "get_space", get_space)
    monkeypatch.setattr(peers, "list_spaces", list_spaces)
    monkeypatch.setattr(peers, "people_by_ids", people_by_ids)
    monkeypatch.setattr(peers, "room_availability", room_availability)
    monkeypatch.setattr(peers, "change_room_limit", change_room_limit)
    monkeypatch.setattr(peers, "room_limit", room_limit)
    monkeypatch.setattr(peers, "send_notification", send_notification)
    monkeypatch.setattr(peers, "audit_append", audit_append)
    monkeypatch.setattr("backend.shared.auth.call_peer", identity_call)
    return fake


@pytest.fixture(scope="session", autouse=True)
def database():
    """独立 SQLite 测试库：只建本模块表。"""
    if _DB_PATH.exists():
        _DB_PATH.unlink()
    engine = session_factory("m2").kw["bind"]
    Base.metadata.create_all(engine)
    yield
    engine.dispose()
    try:
        _DB_PATH.unlink()
    except OSError:
        pass


@pytest.fixture()
def app(database):
    from backend.m2.main import app as m2_app
    from backend.shared.settings import Settings

    m2_app.state.settings = Settings(
        module="m2", app_env="test",
        database_url=f"sqlite+pysqlite:///{_DB_PATH.as_posix()}",
        peer_urls={m: f"http://{m}.test" for m in ("d", "m1", "m2", "m3")},
        credentials={m: f"secret-{m}" for m in ("d", "m1", "m2", "m3")},
        peer_timeout=5,
    )
    return m2_app


@pytest.fixture()
def client(world, app):
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------- 测试辅助
def call(client, function: str, body: dict | None = None, *, account: str | None = None,
         token: str | None = None, expected: int = 200, source: str | None = None):
    """调用 M2 公开端点；返回 data 或（非 200 时）error。

    source="m1" 表示按共同规则由 M1 管理入口以 USER 模式调用（M2-S08 专用配置入口）。
    """
    headers = {}
    credential = token or (TOKEN_OF[account] if account else None)
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    if source:
        headers["X-Service-Credential"] = f"secret-{source}"
        headers["X-Actor-Mode"] = "USER" if credential else "SYSTEM"
    response = client.post(f"/api/v1/m2/{function}", json=body or {}, headers=headers)
    assert response.status_code == expected, f"{function}: {response.status_code} {response.text}"
    envelope = response.json()
    assert set(envelope) == {"ok", "data", "error"}
    assert envelope["ok"] is (expected == 200)
    return envelope["data"] if expected == 200 else envelope["error"]


def calls_of(fake: FakeWorld, module: str, function: str) -> list[dict]:
    return [payload for (mod, fn, payload) in fake.calls if mod == module and fn == function]


def seed_config() -> None:
    """写入共同测试输入的 M2 配置（单元测试用）。"""
    from backend.m2.models import ApprovalConfig

    with session_factory("m2")() as db:
        row = db.get(ApprovalConfig, 1)
        if row is None:
            row = ApprovalConfig(id=1)
            db.add(row)
        fixture = FIXTURE["m2"]
        row.version = fixture["version"]
        row.application_scopes = list(fixture["applicationScopes"])
        row.approval_levels = list(fixture["approvalLevels"])
        row.clock_start = fixture["clockStart"]
        row.timeout_seconds = fixture["timeoutSeconds"]
        row.check_interval_seconds = fixture["checkIntervalSeconds"]
        db.commit()


def clear_config() -> None:
    from backend.m2.models import ApprovalConfig

    with session_factory("m2")() as db:
        row = db.get(ApprovalConfig, 1)
        if row is not None:
            row.version = 1
            row.application_scopes = []
            row.approval_levels = []
            row.clock_start = None
            row.timeout_seconds = None
            row.check_interval_seconds = None
            db.commit()


def slot(hours_ahead: int = 2, hours: int = 1) -> dict:
    from datetime import timedelta

    from backend.shared.clock import format_time, utc_now

    start = utc_now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=hours_ahead)
    return {"startTime": format_time(start), "endTime": format_time(start + timedelta(hours=hours))}
