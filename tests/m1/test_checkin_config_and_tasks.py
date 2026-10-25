import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select

from backend.m1.models import M1Config, ManagementLock, OpeningRange, OpeningSchedule, OutboxEvent, Violation
from backend.m1.seed import seed
from backend.m1.tasks import tick
from backend.shared.db import session_factory
from backend.shared.errors import ApiError
from backend.shared.registry import SERVICES
from tests.m1.test_reservation_and_limits import call, period


def test_check_in_uses_daily_code_window_version_and_permission(m1_client, user_headers):
    reservation = call(m1_client, "reserve_seat", {
        "seatId": "seat-school-1", **period(2, 3), "requestId": str(uuid4())
    }, user_headers)
    denied = call(m1_client, "get_check_in_code", {"spaceId": "sp-school"}, user_headers, 403)
    assert denied["code"] == "FORBIDDEN"
    code = call(m1_client, "get_check_in_code", {"spaceId": "sp-school"},
                {"Authorization": "Bearer screen"})
    request = {"reservationId": reservation["reservationId"], "token": code["token"],
               "expectedVersion": 1, "requestId": str(uuid4())}
    assert call(m1_client, "check_in", request, user_headers, 409)["code"] == "CONFLICT"
    m1_client.app.state.test_clock.write_text("2026-01-01T02:00:00Z", encoding="utf-8")
    checked = call(m1_client, "check_in", request, user_headers)
    assert checked["status"] == "CHECKED_IN" and checked["checkInTime"] == "2026-01-01T02:00:00Z"
    assert call(m1_client, "check_in", request, user_headers) == checked
    changed = request | {"token": "different"}
    assert call(m1_client, "check_in", changed, user_headers, 409)["code"] == "REQUEST_ID_REUSED"


def test_opening_and_config_updates_are_versioned_and_protect_existing_reservations(m1_client, user_headers):
    admin = {"Authorization": "Bearer admin"}
    config = call(m1_client, "get_config", {"owner": "M1"}, admin)
    assert config["config"] == {"maxReservationHours": 4, "version": 1}
    updated = call(m1_client, "update_config", {
        "owner": "M1", "config": {"maxReservationHours": 6}, "expectedVersion": 1,
        "requestId": str(uuid4()),
    }, admin)
    assert updated["config"] == {"maxReservationHours": 6, "version": 2}
    call(m1_client, "reserve_seat", {
        "seatId": "seat-school-1", **period(4, 5), "requestId": str(uuid4())
    }, user_headers)
    conflict = call(m1_client, "set_opening_times", {
        "spaceId": "sp-school", "timeRanges": [{"startTime": "2026-01-01T06:00:00Z",
                                                    "endTime": "2026-01-01T08:00:00Z"}],
        "expectedVersion": 1, "requestId": str(uuid4()),
    }, admin, 409)
    assert conflict["code"] == "CONFLICT"
    opened = call(m1_client, "set_opening_times", {
        "spaceId": "sp-school", "timeRanges": [{"startTime": "2026-01-01T00:00:00Z",
                                                    "endTime": "2026-01-01T12:00:00Z"}],
        "expectedVersion": 1, "requestId": str(uuid4()),
    }, admin)
    assert opened["version"] == 2


def test_tick_cancels_no_show_once_and_persists_outbox(m1_client, user_headers):
    reservation = call(m1_client, "reserve_seat", {
        "seatId": "seat-school-1", **period(2, 3), "requestId": str(uuid4())
    }, user_headers)
    m1_client.app.state.test_clock.write_text("2026-01-01T02:16:00Z", encoding="utf-8")
    asyncio.run(tick())
    asyncio.run(tick())
    cancelled = call(m1_client, "get_my_reservation", {"reservationId": reservation["reservationId"]}, user_headers)
    assert cancelled["status"] == "CANCELLED_NO_SHOW" and cancelled["version"] == 2
    with session_factory("m1")() as db:
        assert db.scalar(select(func.count()).select_from(Violation)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent).where(
            OutboxEvent.status == "SENT"
        )) == 5


def test_usage_totals_do_not_change_with_pagination(m1_client, user_headers):
    for index in range(2):
        call(m1_client, "reserve_seat", {
            "seatId": f"seat-school-{index + 1}", **period(index + 2, index + 3),
            "requestId": str(uuid4()),
        }, user_headers)
    admin = {"Authorization": "Bearer admin"}
    first = call(m1_client, "get_usage", {
        "startTime": "2026-01-01T00:00:00Z", "endTime": "2026-01-02T00:00:00Z",
        "spaceId": "sp-school", "limit": 1,
    }, admin)
    assert first["reservationCount"] == 2
    assert len(first["reservations"]["items"]) == 1
    second = call(m1_client, "get_usage", {
        "startTime": "2026-01-01T00:00:00Z", "endTime": "2026-01-02T00:00:00Z",
        "spaceId": "sp-school", "limit": 1, "cursor": first["reservations"]["nextCursor"],
    }, admin)
    assert second["reservationCount"] == 2


def test_seed_is_repeatable_and_does_not_overwrite_existing_config(m1_client, monkeypatch):
    async def verified(_):
        return None
    monkeypatch.setattr("backend.m1.seed._verify_master_data", verified)
    with session_factory("m1")() as db:
        db.query(OpeningRange).delete()
        db.query(OpeningSchedule).delete()
        config = db.get(M1Config, "default")
        config.max_reservation_hours = 7
        config.version = 3
        db.commit()
    seed()
    seed()
    with session_factory("m1")() as db:
        config = db.get(M1Config, "default")
        assert (config.max_reservation_hours, config.version) == (7, 3)
        assert db.scalar(select(func.count()).select_from(OpeningSchedule)) == 2
        assert db.scalar(select(func.count()).select_from(OpeningRange)) == 14


def test_unknown_master_update_keeps_reservation_lock(m1_client, user_headers, monkeypatch):
    import backend.m1.api as api_module
    original = api_module.call_peer

    async def unavailable(settings, target, function, payload, actor):
        if function == "maintain_master":
            raise ApiError("UNAVAILABLE", "结果未知")
        return await original(settings, target, function, payload, actor)

    monkeypatch.setattr(api_module, "call_peer", unavailable)
    admin = {"Authorization": "Bearer admin"}
    request_id = str(uuid4())
    error = call(m1_client, "maintain_master", {
        "operation": "UPDATE_SEAT", "seatId": "seat-school-1", "seatCode": "S-1",
        "enabled": True, "socketTags": ["FIXED"], "expectedVersion": 1, "requestId": request_id,
    }, admin, 503)
    assert error["code"] == "UNAVAILABLE"
    with session_factory("m1")() as db:
        lock = db.get(ManagementLock, "seat:seat-school-1")
        assert lock is not None and lock.request_id == request_id
    conflict = call(m1_client, "reserve_seat", {
        "seatId": "seat-school-1", **period(5, 6), "requestId": str(uuid4())
    }, user_headers, 409)
    assert conflict["code"] == "CONFLICT"


def test_all_m1_contract_routes_and_frontend_pages_are_present():
    from backend.m1.main import app

    assert app.state.implemented == set(SERVICES["m1"])
    root = Path(__file__).resolve().parents[2]
    for name in ("index.html", "screen.html"):
        text = (root / "frontend" / "m1" / name).read_text(encoding="utf-8")
        assert "../shared/auth.js" in text
