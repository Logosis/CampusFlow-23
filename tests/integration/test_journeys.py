"""真实HTTP集成验收；只在全新隔离测试库运行，不得用模拟服务冒充通过。"""
import json
import subprocess
import sys
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


def audits(admin, source, business_id):
    return call("d", "audit", {"operation": "LIST", "sourceModule": source,
                "businessId": business_id, "limit": 100}, admin)["items"]


def has_event(action, actor_id):
    def check(events):
        assert any(event["action"] == action and event["actorId"] == actor_id for event in events)
    return check


def test_m2_cancel_query_audit_and_real_email():
    student, admin = login("student1"), login("admin")
    purpose = "集成撤销验收-" + rid()
    submitted = call("m2", "submit_application", {"spaceId": "sp-school", **slot(8),
                     "purpose": purpose, "requestId": rid()}, student)
    app = submitted["application"]
    body = {"applicationId": app["applicationId"], "expectedVersion": app["version"], "requestId": rid()}
    cancelled = call("m2", "cancel_application", body, student)
    assert cancelled["application"]["status"] == "CANCELLED"
    assert call("m2", "cancel_application", body, student) == cancelled
    eventually(lambda: call("m2", "get_application", {"applicationId": app["applicationId"]}, student), sync_success)
    for view, token in [("OWN", student), ("MANAGED", admin)]:
        page = call("m2", "get_applications", {"view": view, "applicationId": app["applicationId"]}, token)
        assert len(page["items"]) == 1 and page["items"][0]["application"]["status"] == "CANCELLED"
    eventually(lambda: audits(admin, "M2", app["applicationId"]), has_event("CANCEL_APPLICATION", "u-student1"))
    notification = next(n["notification"] for n in cancelled["notifications"]
                        if n["notification"] and n["notification"]["kind"] == "M2_CANCELLED")
    def delivered(result):
        assert result["deliveries"] and all(d["status"] == "SUBMITTED" for d in result["deliveries"])
    eventually(lambda: call("d", "get_notification", {"notificationId": notification["notificationId"]}, admin), delivered)
    from dotenv import load_dotenv
    import os
    load_dotenv(ROOT / ".env", override=False)
    mailpit = os.environ.get("MAILPIT_BASE_URL")
    assert mailpit, "真实邮件验收需要隔离Mailpit"
    messages = httpx.get(mailpit + "/api/v1/messages", trust_env=False, timeout=5).json()["messages"]
    matching = [m for m in messages if m["Subject"] == notification["title"]]
    assert any(purpose in httpx.get(mailpit + "/api/v1/message/" + m["ID"], trust_env=False, timeout=5).json()["Text"]
               for m in matching), "Mailpit未收到对应业务邮件"


def test_m3_cancel_query_progress_and_statistics_with_initial_roles():
    student, admin = login("student1"), login("admin")
    start = utc_now()
    reported = call("m3", "report_fault", {"spaceId": "sp-school", "faultLocation": "走廊",
                    "description": "集成维修前取消", "requestId": rid()}, student)
    work = reported["workOrder"]
    result = call("m3", "cancel_work_order", {"workOrderId": work["workOrderId"],
                  "expectedVersion": work["version"], "requestId": rid()}, student)
    assert result["workOrder"]["status"] == "CANCELLED"
    detail = call("m3", "get_work_order", {"workOrderId": work["workOrderId"]}, student)
    assert [event["type"] for event in detail["timeline"]] == ["REPORT", "CANCEL"]
    page = call("m3", "get_work_orders", {"view": "OWN", "status": "CANCELLED"}, student)
    assert any(w["workOrderId"] == work["workOrderId"] for w in page["items"])
    stats = call("m3", "get_maintenance_stats", {"startTime": format_time(start),
                 "endTime": format_time(start + timedelta(seconds=1))}, admin)
    assert stats["statusCounts"]["CANCELLED"] >= 1
    call("m3", "get_maintenance_stats", {"startTime": format_time(start),
         "endTime": format_time(start + timedelta(seconds=1))}, student, expected=403)
    eventually(lambda: audits(admin, "M3", work["workOrderId"]), has_event("CANCEL", "u-student1"))


def test_real_timeout_config_and_system_audits():
    admin, student = login("admin"), login("student1")
    originals = {owner: call("m1", "get_config", {"owner": owner}, admin)["config"] for owner in ("M2", "M3")}
    def update(owner, config):
        current = call("m1", "get_config", {"owner": owner}, admin)["config"]
        fields = {k: v for k, v in config.items() if k != "version"}
        return call("m1", "update_config", {"owner": owner, "config": fields,
                    "expectedVersion": current["version"], "requestId": rid()}, admin)
    try:
        update("M2", originals["M2"] | {"timeoutSeconds": 10, "checkIntervalSeconds": 1})
        update("M3", originals["M3"] | {"checkIntervalSeconds": 1,
               "rules": [r | {"timeoutSeconds": 10} for r in originals["M3"]["rules"]]})
        app = call("m2", "submit_application", {"spaceId": "sp-school", **slot(9),
                   "purpose": "集成超时撤销", "requestId": rid()}, student)["application"]
        work = call("m3", "report_fault", {"spaceId": "sp-school", "faultLocation": "讲台",
                    "description": "集成超时升级", "requestId": rid()}, student)["workOrder"]
        subprocess.run([sys.executable, "scripts/clock.py", "advance", "--seconds", "15"], cwd=ROOT, check=True)
        def timed_out(result):
            assert result["application"]["status"] == "TIMEOUT_CANCELLED"
            sync_success(result)
        eventually(lambda: call("m2", "get_application", {"applicationId": app["applicationId"]}, student), timed_out)
        eventually(lambda: audits(admin, "M2", app["applicationId"]), has_event("TIMEOUT_CANCEL", "M2"))
        def escalated(detail):
            assert len(detail["detail"]["escalations"]) == 1
        eventually(lambda: call("m3", "get_work_order", {"workOrderId": work["workOrderId"]}, admin), escalated)
        eventually(lambda: audits(admin, "M3", work["workOrderId"]), has_event("ESCALATE", "M3"))
    finally:
        for owner, config in originals.items():
            update(owner, config)


def test_pending_approval_keeps_rules_after_real_admin_config_change():
    admin, student, first, last = [login(x) for x in ("admin", "student1", "approver1", "approver2")]
    original = call("m1", "get_config", {"owner": "M2"}, admin)["config"]
    app = call("m2", "submit_application", {"spaceId": "sp-school", **slot(10),
               "purpose": "配置变更保留在途规则", "requestId": rid()}, student)["application"]
    def change(config):
        version = call("m1", "get_config", {"owner": "M2"}, admin)["config"]["version"]
        return call("m1", "update_config", {"owner": "M2", "config": {k: v for k, v in config.items() if k != "version"},
                    "expectedVersion": version, "requestId": rid()}, admin)
    try:
        change(original | {"approvalLevels": [{"level": 1, "approverId": "u-approver2"},
                           {"level": 2, "approverId": "u-admin"}], "timeoutSeconds": 60})
        tasks = call("m2", "get_approval_tasks", {"applicationId": app["applicationId"]}, first)
        assert len(tasks["items"]) == 1
        one = call("m2", "submit_approval", {"applicationId": app["applicationId"], "level": 1,
                   "decision": "APPROVE", "expectedVersion": app["version"], "requestId": rid()}, first)
        two = call("m2", "submit_approval", {"applicationId": app["applicationId"], "level": 2,
                   "decision": "APPROVE", "expectedVersion": one["application"]["version"], "requestId": rid()}, last)
        assert two["application"]["status"] == "APPROVED"
        sync_success(two)
    finally:
        change(original)
