from uuid import uuid4


def call(client, function, body, headers, expected=200):
    response = client.post(f"/api/v1/m1/{function}", json=body, headers=headers)
    assert response.status_code == expected, response.text
    envelope = response.json()
    assert set(envelope) == {"ok", "data", "error"}
    return envelope["data"] if expected == 200 else envelope["error"]


def period(start=2, end=3):
    return {"startTime": f"2026-01-01T{start:02}:00:00Z", "endTime": f"2026-01-01T{end:02}:00:00Z"}


def test_reservation_checks_department_conflict_and_idempotency(m1_client, user_headers):
    request = {"seatId": "seat-school-1", **period(), "requestId": str(uuid4())}
    created = call(m1_client, "reserve_seat", request, user_headers)
    assert created["status"] == "RESERVED"
    assert call(m1_client, "reserve_seat", request, user_headers) == created
    reused = call(m1_client, "reserve_seat", request | {"seatId": "seat-school-2"}, user_headers, 409)
    assert reused["code"] == "REQUEST_ID_REUSED"
    conflict = call(m1_client, "reserve_seat", request | {"requestId": str(uuid4())},
                    {"Authorization": "Bearer student2"}, 409)
    assert conflict["code"] == "CONFLICT"
    forbidden = call(m1_client, "reserve_seat", {
        "seatId": "seat-cs-1", **period(4, 5), "requestId": str(uuid4())
    }, {"Authorization": "Bearer student2"}, 403)
    assert forbidden["code"] == "FORBIDDEN"
    detail = call(m1_client, "get_my_reservation", {"reservationId": created["reservationId"]}, user_headers)
    assert detail == created


def test_m2_limit_blocks_reservation_and_release_is_versioned(m1_client, user_headers, module_headers):
    business_id = "app-1"
    occupy = {"source": "M2", "businessId": business_id, "spaceId": "sp-school", "action": "OCCUPY",
              "businessState": "APPROVED", **period(4, 5), "sourceVersion": 2, "requestId": str(uuid4())}
    result = call(m1_client, "change_room_limit", occupy, module_headers("m2"))
    assert result["currentLimit"]["state"] == "ACTIVE"
    availability = call(m1_client, "get_room_availability", {"spaceId": "sp-school", **period(4, 5)},
                        module_headers("m2"))
    assert availability["blockedBy"] == ["M2"]
    call(m1_client, "reserve_seat", {
        "seatId": "seat-school-1", **period(4, 5), "requestId": str(uuid4())
    }, user_headers, 409)
    release = occupy | {"action": "RELEASE", "businessState": "CANCELLED", "sourceVersion": 3,
                        "requestId": str(uuid4())}
    released = call(m1_client, "change_room_limit", release, module_headers("m2"))
    assert released["currentLimit"]["state"] == "RELEASED"
    stale = occupy | {"sourceVersion": 2, "requestId": str(uuid4())}
    assert call(m1_client, "change_room_limit", stale, module_headers("m2"), 409)["code"] == "CONFLICT"
    queried = call(m1_client, "get_room_limit", {"source": "M2", "businessId": business_id}, module_headers("m2"))
    assert queried["maxProcessedVersion"] == 3
    assert queried["currentLimit"]["state"] == "RELEASED"


def test_m3_repair_cancels_personal_booking_but_preserves_m2_limit(m1_client, user_headers, module_headers):
    reservation = call(m1_client, "reserve_seat", {
        "seatId": "seat-school-2", **period(3, 4), "requestId": str(uuid4())
    }, user_headers)
    m2 = {"source": "M2", "businessId": "app-keep", "spaceId": "sp-school", "action": "OCCUPY",
          "businessState": "APPROVED", **period(6, 7), "sourceVersion": 1, "requestId": str(uuid4())}
    call(m1_client, "change_room_limit", m2, module_headers("m2"))
    repair = {"source": "M3", "businessId": "work-1", "spaceId": "sp-school", "action": "REPAIR",
              "businessState": "REPAIRING", "startTime": None, "endTime": None,
              "sourceVersion": 2, "requestId": str(uuid4())}
    call(m1_client, "change_room_limit", repair, module_headers("m3"))
    search = call(m1_client, "search_seats", {"limit": 100}, user_headers)
    assert all(item["space"]["spaceId"] != "sp-school" for item in search["items"])
    cancelled = call(m1_client, "get_my_reservation", {"reservationId": reservation["reservationId"]}, user_headers)
    assert cancelled["status"] == "CANCELLED_MAINTENANCE"
    blocked = call(m1_client, "get_room_availability", {"spaceId": "sp-school", **period(6, 7)},
                   module_headers("m2"))
    assert blocked["blockedBy"] == ["M2", "M3"]
    release = repair | {"action": "RELEASE", "businessState": "ACCEPTED", "sourceVersion": 3,
                        "requestId": str(uuid4())}
    call(m1_client, "change_room_limit", release, module_headers("m3"))
    after = call(m1_client, "get_room_availability", {"spaceId": "sp-school", **period(6, 7)},
                 module_headers("m2"))
    assert after["blockedBy"] == ["M2"]


def test_release_tombstone_prevents_late_repair_and_forged_source(m1_client, module_headers):
    release = {"source": "M3", "businessId": "work-late", "spaceId": "sp-school", "action": "RELEASE",
               "businessState": "ACCEPTED", "startTime": None, "endTime": None,
               "sourceVersion": 4, "requestId": str(uuid4())}
    done = call(m1_client, "change_room_limit", release, module_headers("m3"))
    assert done["currentLimit"]["state"] == "RELEASED"
    late = release | {"action": "REPAIR", "businessState": "REPAIRING", "sourceVersion": 3,
                      "requestId": str(uuid4())}
    assert call(m1_client, "change_room_limit", late, module_headers("m3"), 409)["code"] == "CONFLICT"
    assert call(m1_client, "change_room_limit", release | {"requestId": str(uuid4())},
                module_headers("m2"), 403)["code"] == "FORBIDDEN"
