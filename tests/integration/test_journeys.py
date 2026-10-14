"""真实HTTP集成验收；当前业务尚未实现，这些测试应失败，不得用模拟服务冒充通过。"""
import json
import time
from pathlib import Path
from datetime import timedelta
from uuid import uuid4
import httpx
import pytest
from backend.shared.clock import utc_now, format_time
from backend.shared.settings import Settings

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "tests/fixtures/bootstrap.json").read_text(encoding="utf-8"))

def rid():
    return str(uuid4())

def call(module, function, body, token=None, source=None, expected=200):
    settings = Settings.load(module)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if source:
        headers["X-Service-Credential"] = settings.credentials[source]
        headers["X-Actor-Mode"] = "USER" if token else "SYSTEM"
    response = httpx.post(settings.peer_urls[module] + f"/api/v1/{module}/{function}",
                          json=body, headers=headers, timeout=10, trust_env=False)
    assert response.status_code == expected, f"{module}.{function}: {response.status_code} {response.text}"
    envelope = response.json()
    assert set(envelope) == {"ok", "data", "error"}
    assert envelope["ok"] is (expected == 200)
    return envelope["data"] if expected == 200 else envelope["error"]

def login(account):
    return call("d", "login", {"account": account, "password": FIXTURE["password"], "requestId": rid()})["credential"]

def slot(offset_hours=2):
    now = utc_now()
    start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset_hours)
    return {"startTime": format_time(start), "endTime": format_time(start + timedelta(hours=1))}

def eventually(getter, assertion, seconds=10):
    deadline = time.monotonic() + seconds
    while True:
        result = getter()
        try:
            assertion(result)
            return result
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)

def sync_success(result):
    assert result["seatSync"]["status"] == "SUCCESS"
    assert result["seatSync"]["result"]["requestStatus"] == "SUCCESS"

@pytest.fixture(scope="module", autouse=True)
def require_real_test_environment():
    assert Settings.load("d").app_env == "test", "仅使用隔离APP_ENV=test环境"
    for module in ("d", "m1", "m2", "m3"):
        data = httpx.get(Settings.load(module).peer_urls[module] + "/health", timeout=5, trust_env=False).json()["data"]
        assert data["unfinishedFunctions"] == [], f"{module}必做服务尚未实现"
        assert data["workerConfigured"] and not data["workerError"]

def test_identity_department_scope_and_booking_idempotence():
    student, other = login("student1"), login("student2")
    period = slot()
    request = {"seatId": "seat-school-1", **period, "requestId": rid()}
    reservation = call("m1", "reserve_seat", request, student)
    assert reservation["status"] == "RESERVED"
    assert call("m1", "reserve_seat", request, student) == reservation
    error = call("m1", "reserve_seat", request | {"seatId": "seat-school-2"}, student, expected=409)
    assert error["code"] == "REQUEST_ID_REUSED"
    call("m1", "reserve_seat", request | {"requestId": rid()}, other, expected=409)
    call("m1", "reserve_seat", request | {"seatId": "seat-cs-1", "requestId": rid()}, other, expected=403)
    call("m1", "get_check_in_code", {"spaceId": "sp-school"}, student, expected=403)
    history = call("m1", "get_my_reservation", {"reservationId": reservation["reservationId"]}, student)
    assert history["reservationId"] == reservation["reservationId"]

def approve_room(applicant, period):
    first, last = login("approver1"), login("approver2")
    submitted = call("m2", "submit_application", {"spaceId": "sp-school", **period,
                     "purpose": "集成测试审批", "requestId": rid()}, applicant)
    application = submitted["application"]
    assert application["status"] == "PENDING" and submitted["seatSync"] is None
    one = call("m2", "submit_approval", {"applicationId": application["applicationId"], "level": 1,
               "decision": "APPROVE", "expectedVersion": application["version"], "requestId": rid()}, first)
    assert one["application"]["status"] == "PENDING" and one["seatSync"] is None
    two = call("m2", "submit_approval", {"applicationId": application["applicationId"], "level": 2,
               "decision": "APPROVE", "expectedVersion": one["application"]["version"], "requestId": rid()}, last)
    assert two["application"]["status"] == "APPROVED"
    eventually(lambda: call("m2", "get_application", {"applicationId": application["applicationId"]}, applicant), sync_success)
    return application["applicationId"]

def test_two_level_approval_occupies_only_after_final_decision():
    applicant = login("student1")
    period = slot(4)
    approve_room(applicant, period)
    blocked = call("m1", "get_room_availability", {"spaceId": "sp-school", **period}, source="m2")
    assert not blocked["canOccupy"] and "M2" in blocked["blockedBy"]
    call("m1", "reserve_seat", {"seatId": "seat-school-1", **period, "requestId": rid()}, applicant, expected=409)

def test_repair_keeps_restriction_until_acceptance_and_preserves_other_sources():
    reporter, dispatcher, technician, inspector = [login(x) for x in ("student1", "dispatcher", "technician", "inspector")]
    # 本场景自己建立两种来源，不依赖其他测试的执行顺序。
    reservation = call("m1", "reserve_seat", {"seatId": "seat-school-2", **slot(3), "requestId": rid()}, reporter)
    period = slot(6)
    approve_room(reporter, period)
    result = call("m3", "report_fault", {"spaceId": "sp-school", "faultLocation": "门口",
                 "description": "集成测试维修", "requestId": rid()}, reporter)
    work = result["workOrder"]
    result = call("m3", "dispatch_order", {"workOrderId": work["workOrderId"], "assigneeId": "u-technician",
                 "expectedVersion": work["version"], "requestId": rid()}, dispatcher)
    result = call("m3", "start_repair", {"workOrderId": work["workOrderId"],
                 "expectedVersion": result["workOrder"]["version"], "requestId": rid()}, technician)
    def detail(purpose="REPAIR", token=technician):
        return call("m3", "get_work_tasks", {"purpose": purpose, "workOrderId": work["workOrderId"]}, token)["items"][0]
    eventually(detail, sync_success)
    cancelled = call("m1", "get_my_reservation", {"reservationId": reservation["reservationId"]}, reporter)
    assert cancelled["status"] == "CANCELLED_MAINTENANCE"
    assert cancelled["checkInTime"] == reservation["checkInTime"]
    occupied = call("m1", "get_room_availability", {"spaceId": "sp-school", **period}, source="m2")
    assert "M3" in occupied["blockedBy"]
    result = call("m3", "submit_completion", {"workOrderId": work["workOrderId"], "summary": "已修复",
                 "expectedVersion": result["workOrder"]["version"], "requestId": rid()}, technician)
    assert result["workOrder"]["status"] == "WAITING_ACCEPTANCE"
    assert "M3" in call("m1", "get_room_availability", {"spaceId": "sp-school", **period}, source="m2")["blockedBy"]
    completion = detail("ACCEPTANCE", inspector)["completions"][-1]
    result = call("m3", "submit_acceptance", {"workOrderId": work["workOrderId"], "completionId": completion["completionId"],
                 "decision": "PASS", "expectedVersion": result["workOrder"]["version"], "requestId": rid()}, inspector)
    assert result["workOrder"]["status"] == "ACCEPTED"
    eventually(lambda: detail("ACCEPTANCE", inspector), sync_success)
    room = call("m1", "get_room_availability", {"spaceId": "sp-school", **period}, source="m2")
    assert "M3" not in room["blockedBy"] and "M2" in room["blockedBy"]
    call("m3", "submit_evaluation", {"workOrderId": work["workOrderId"], "content": "验收正常", "requestId": rid()}, reporter)

def test_release_tombstone_rejects_late_request_and_untrusted_source():
    business = "integration-" + rid()
    request = {"source": "M3", "businessId": business, "spaceId": "sp-school", "action": "RELEASE",
               "businessState": "ACCEPTED", "startTime": None, "endTime": None, "sourceVersion": 4, "requestId": rid()}
    result = call("m1", "change_room_limit", request, source="m3")
    assert result["currentLimit"]["state"] == "RELEASED"
    call("m1", "change_room_limit", request | {"action": "REPAIR", "businessState": "REPAIRING",
         "sourceVersion": 3, "requestId": rid()}, source="m3", expected=409)
    assert call("m1", "get_room_limit", {"source": "M3", "businessId": business}, source="m3")["currentLimit"]["state"] == "RELEASED"
    call("m1", "change_room_limit", request | {"requestId": rid()}, source="m2", expected=403)
