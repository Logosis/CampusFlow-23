from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.m1.api import router
from backend.m1.models import Base, M1Config, OpeningRange, OpeningSchedule
from backend.shared.db import session_factory
from backend.shared.factory import create_app
from backend.shared.settings import Settings


SPACE_SCHOOL = {
    "spaceId": "sp-school", "name": "公共自习室", "location": "测试楼101",
    "isStudyRoom": True, "ownerKind": "SCHOOL", "departmentId": None,
    "registered": True, "enabled": True, "version": 1,
}
SPACE_CS = {
    "spaceId": "sp-cs", "name": "计算机系自习室", "location": "测试楼102",
    "isStudyRoom": True, "ownerKind": "DEPARTMENT", "departmentId": "dep-cs",
    "registered": True, "enabled": True, "version": 1,
}
SEATS = [
    {"seatId": "seat-school-1", "seatCode": "S-1", "spaceId": "sp-school", "registered": True,
     "enabled": True, "socketTags": ["FIXED"], "version": 1},
    {"seatId": "seat-school-2", "seatCode": "S-2", "spaceId": "sp-school", "registered": True,
     "enabled": True, "socketTags": ["RAIL"], "version": 1},
    {"seatId": "seat-cs-1", "seatCode": "C-1", "spaceId": "sp-cs", "registered": True,
     "enabled": True, "socketTags": [], "version": 1},
]


@pytest.fixture()
def m1_client(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "m1.sqlite"
    database_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("M1_DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "test")
    clock = tmp_path / "clock.txt"
    clock.write_text("2026-01-01T00:00:00Z", encoding="utf-8")
    monkeypatch.setenv("TEST_CLOCK_FILE", str(clock))
    session_factory.cache_clear()
    factory = session_factory("m1")
    Base.metadata.create_all(factory.kw["bind"])
    with factory() as db:
        db.add(M1Config(key="default", max_reservation_hours=4, version=1))
        for space_id in ("sp-school", "sp-cs"):
            schedule = OpeningSchedule(space_id=space_id, version=1)
            schedule.ranges.append(OpeningRange(
                start_time=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc),
            ))
            db.add(schedule)
        db.commit()

    identities = {
        "admin": {"userId": "u-admin", "displayName": "admin", "departmentId": None,
                  "accountStatus": "ACTIVE", "roleIds": [], "grants": []},
        "student1": {"userId": "u-student1", "displayName": "student1", "departmentId": "dep-cs",
                     "accountStatus": "ACTIVE", "roleIds": [], "grants": []},
        "student2": {"userId": "u-student2", "displayName": "student2", "departmentId": "dep-ee",
                     "accountStatus": "ACTIVE", "roleIds": [], "grants": []},
        "screen": {"userId": "u-screen", "displayName": "screen", "departmentId": None,
                   "accountStatus": "ACTIVE", "roleIds": [], "grants": []},
    }

    async def fake_peer(settings, target, function, payload, actor):
        assert target == "d"
        if function == "get_identity":
            return identities[actor.user_credential]
        if function == "check_permission":
            restricted = {
                "m1.check_in_code.read": {"screen", "admin"},
                "m1.config.read": {"admin"}, "m1.config.manage": {"admin"},
                "m1.opening.read": {"admin"}, "m1.opening.manage": {"admin"},
                "m1.usage.read": {"admin"},
            }
            allowed = actor.user_credential in restricted.get(payload["permissionCode"], identities)
            return {"subjectType": "USER", "subjectId": identities[actor.user_credential]["userId"],
                    "allowed": allowed,
                    "allowedScopes": [{"type": "GLOBAL", "id": None}] if allowed else [],
                    "reason": "" if allowed else "测试权限拒绝"}
        if function == "send_notification":
            return {"notificationId": f"notification-{payload['requestId']}", **payload, "channel": "EMAIL",
                    "createdAt": "2026-01-01T00:00:00Z", "deliveries": []}
        if function == "get_notification":
            return {"notificationId": "notification", "requestId": payload["requestId"]}
        if function == "audit":
            return {"auditId": f"audit-{payload['requestId']}"}
        if function == "maintain_master":
            if payload["operation"] == "UPDATE_SEAT":
                return next(item for item in SEATS if item["seatId"] == payload["seatId"]) | {
                    "seatCode": payload["seatCode"], "enabled": payload["enabled"],
                    "socketTags": payload["socketTags"], "version": payload["expectedVersion"] + 1,
                }
            raise AssertionError(payload["operation"])
        if function != "query_master":
            raise AssertionError(function)
        operation = payload["operation"]
        if operation == "GET_SPACE":
            return {"sp-school": SPACE_SCHOOL, "sp-cs": SPACE_CS}[payload["spaceId"]]
        if operation == "GET_SEAT":
            return next(item for item in SEATS if item["seatId"] == payload["seatId"])
        if operation == "LIST_SEATS":
            items = [item for item in SEATS if (not payload.get("spaceId") or item["spaceId"] == payload["spaceId"])]
            if payload.get("socketTag"):
                items = [item for item in items if payload["socketTag"] in item["socketTags"]]
            return {"items": items, "nextCursor": None}
        raise AssertionError(operation)

    monkeypatch.setattr("backend.shared.auth.call_peer", fake_peer)
    monkeypatch.setattr("backend.m1.api.call_peer", fake_peer)
    monkeypatch.setattr("backend.m1.tasks.call_peer", fake_peer)
    app = create_app("m1", router)
    app.state.settings = Settings(
        "m1", "test", database_url,
        {name: f"http://{name}" for name in ("d", "m1", "m2", "m3")},
        {name: f"secret-{name}" for name in ("d", "m1", "m2", "m3")}, 5,
    )
    app.state.test_clock = clock
    with TestClient(app) as client:
        yield client
    session_factory.cache_clear()


@pytest.fixture()
def user_headers():
    return {"Authorization": "Bearer student1"}


@pytest.fixture()
def module_headers():
    def make(module):
        return {"X-Service-Credential": f"secret-{module}", "X-Actor-Mode": "SYSTEM"}
    return make
