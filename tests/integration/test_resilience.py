"""显式启用的独立Compose故障验收；真实提供方、真实数据库，无成功响应替身。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from threading import Event, Lock, Thread, Barrier
import time

import httpx
import pytest

from backend.shared.clock import utc_now, format_time, parse_time
from backend.shared.settings import Settings
from tests.integration.test_journeys import call, login, rid, slot, eventually, sync_success

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def compose():
    project = os.environ.get("CAMPUSFLOW_TEST_PROJECT", "")
    if not project:
        pytest.skip("需显式指定独立CAMPUSFLOW_TEST_PROJECT才能控制容器")
    assert project.startswith("campusflow23-test-") and Settings.load("d").app_env == "test"
    docker = os.environ.get("CAMPUSFLOW_TEST_DOCKER", "docker")
    base = [docker, "compose", "--project-directory", str(ROOT), "--env-file", str(ROOT / ".env"),
            "-p", project, "-f", str(ROOT / "compose.yaml")]
    for service in ("d", "m1", "m2", "m3"):
        ident = subprocess.check_output(base + ["ps", "-q", service], text=True).strip()
        assert ident, service
        info = json.loads(subprocess.check_output([docker, "inspect", ident], text=True))[0]
        assert info["Config"]["Labels"]["com.docker.compose.project"] == project
        assert "APP_ENV=test" in info["Config"]["Env"]
    def invoke(*arguments, override=None):
        extra = ["-f", str(override)] if override else []
        return subprocess.run(base + extra + list(arguments), capture_output=True, text=True, check=True, timeout=90).stdout
    return invoke


def ready(module, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(Settings.load(module).peer_urls[module] + "/health", trust_env=False, timeout=2)
            if response.status_code == 200 and not response.json()["data"]["workerError"]:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.2)
    pytest.fail(f"{module}未在期限内恢复就绪")


def private_room():
    admin = login("admin")
    space = call("m1", "maintain_master", {"operation": "CREATE_SPACE", "name": "隔离并发验收-" + rid(),
        "location": "仅独立测试库", "isStudyRoom": True, "ownerKind": "SCHOOL", "departmentId": None,
        "enabled": True, "requestId": rid()}, admin)
    seat = call("m1", "maintain_master", {"operation": "CREATE_SEAT", "spaceId": space["spaceId"],
        "seatCode": "CHECK-" + rid(), "enabled": True, "socketTags": [], "requestId": rid()}, admin)
    start = utc_now().replace(minute=0, second=0, microsecond=0)
    call("m1", "set_opening_times", {"spaceId": space["spaceId"], "expectedVersion": 1, "requestId": rid(),
         "timeRanges": [{"startTime": format_time(start), "endTime": format_time(start + timedelta(days=2))}]}, admin)
    return space["spaceId"], seat["seatId"]


def post(module, function, body, token):
    return httpx.post(Settings.load(module).peer_urls[module] + f"/api/v1/{module}/{function}",
                      json=body, headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=7)


@contextmanager
def delayed_people_proxy():
    """仅延迟真实D人员核验响应，确保两个审批请求实际重叠。"""
    held, release, used = Event(), Event(), Event()
    claim = Lock()
    upstream = Settings.load("d").peer_urls["d"]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {k: self.headers[k] for k in ("Authorization", "X-Service-Credential", "X-Actor-Mode", "Content-Type") if k in self.headers}
            response = httpx.post(upstream + self.path, content=body, headers=headers, trust_env=False, timeout=5)
            delay = False
            if self.path.endswith("/query_people"):
                with claim:
                    if not used.is_set():
                        used.set(); delay = True
            if delay:
                held.set()
                release.wait(5)
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield server.server_port, held, release
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join(3)


def test_overlapping_approvals_do_not_block_server(compose):
    space, _ = private_room()
    student, approver = login("student1"), login("approver1")
    app = call("m2", "submit_application", {"spaceId": space, **slot(),
               "purpose": "审批真实重叠", "requestId": rid()}, student)["application"]
    body = {"applicationId": app["applicationId"], "level": 1, "decision": "APPROVE",
            "expectedVersion": app["version"], "requestId": rid()}
    override = ROOT / "tmp" / ("fault-" + rid() + ".json")
    try:
        with delayed_people_proxy() as (port, held, release):
            override.write_text(json.dumps({"services": {"m2": {"environment": {
                "D_BASE_URL": f"http://host.docker.internal:{port}"}}}}), encoding="utf-8")
            compose("up", "-d", "--no-deps", "--force-recreate", "m2", override=override)
            ready("m2")
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(post, "m2", "submit_approval", body, approver)
                assert held.wait(4), "未到达真实D响应延迟点"
                second = pool.submit(post, "m2", "submit_approval", body | {"requestId": rid()}, approver)
                time.sleep(.2); release.set()
                results = [first.result(timeout=9), second.result(timeout=9)]
            assert sorted(r.status_code for r in results) == [200, 409]
            detail = call("m2", "get_application", {"applicationId": app["applicationId"]}, student)
            assert len(detail["approvals"]) == 1
    finally:
        compose("stop", "-t", "1", "m2")
        compose("up", "-d", "--no-deps", "--force-recreate", "m2")
        ready("m2")


def race(*operations):
    barrier = Barrier(len(operations))
    def invoke(operation):
        barrier.wait(timeout=5)
        return operation()
    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(invoke, operations))


def advance(seconds):
    subprocess.run([os.sys.executable, "scripts/clock.py", "advance", "--seconds", str(seconds)], cwd=ROOT, check=True)


def test_competing_bookings_and_same_request_create_only_once(compose):
    _, seat = private_room()
    first, second = login("student1"), login("student2")
    body = {"seatId": seat, **slot(), "requestId": rid()}
    results = race(lambda: post("m1", "reserve_seat", body, first),
                   lambda: post("m1", "reserve_seat", body | {"requestId": rid()}, second))
    assert sorted(r.status_code for r in results) == [200, 409]
    repeated = body | slot(3) | {"requestId": rid()}
    results = race(lambda: post("m1", "reserve_seat", repeated, first),
                   lambda: post("m1", "reserve_seat", repeated, first))
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json()["data"] == results[1].json()["data"]


def test_final_approval_and_timeout_have_only_one_outcome(compose):
    space, _ = private_room()
    admin, student, first, last = [login(x) for x in ("admin", "student1", "approver1", "approver2")]
    original = call("m1", "get_config", {"owner": "M2"}, admin)["config"]
    def configure(config):
        version = call("m1", "get_config", {"owner": "M2"}, admin)["config"]["version"]
        call("m1", "update_config", {"owner": "M2", "config": {k: v for k, v in config.items() if k != "version"},
             "expectedVersion": version, "requestId": rid()}, admin)
    try:
        configure(original | {"timeoutSeconds": 5, "checkIntervalSeconds": 1})
        app = call("m2", "submit_application", {"spaceId": space, **slot(), "purpose": "审批超时竞争", "requestId": rid()}, student)["application"]
        one = call("m2", "submit_approval", {"applicationId": app["applicationId"], "level": 1,
                   "decision": "APPROVE", "expectedVersion": app["version"], "requestId": rid()}, first)
        body = {"applicationId": app["applicationId"], "level": 2, "decision": "APPROVE",
                "expectedVersion": one["application"]["version"], "requestId": rid()}
        response, _ = race(lambda: post("m2", "submit_approval", body, last), lambda: advance(6))
        def finished(result):
            assert result["application"]["status"] in {"APPROVED", "TIMEOUT_CANCELLED"}
            sync_success(result)
        final = eventually(lambda: call("m2", "get_application", {"applicationId": app["applicationId"]}, student), finished)
        approved = final["application"]["status"] == "APPROVED"
        assert response.status_code == (200 if approved else 409)
        assert len(final["approvals"]) == (2 if approved else 1)
        limit = call("m1", "get_room_limit", {"source": "M2", "businessId": app["applicationId"]}, source="m2")
        assert limit["currentLimit"]["state"] == ("ACTIVE" if approved else "RELEASED")
    finally:
        configure(original)


def outbox_status(compose, request_id):
    code = "import json,sys; from sqlalchemy import select; from backend.shared.db import session_factory; from backend.m1.models import OutboxEvent; " \
           "db=session_factory('m1')(); row=db.scalar(select(OutboxEvent).where(OutboxEvent.request_id==sys.argv[1])); print(json.dumps(row.status if row else None)); db.close()"
    return json.loads(compose("exec", "-T", "m1", "uv", "run", "--no-sync", "python", "-c", code, request_id))


def unconfirmed_m1_outbox(compose):
    code = "from sqlalchemy import select,func; from backend.shared.db import session_factory; from backend.m1.models import OutboxEvent; " \
           "db=session_factory('m1')(); print(db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status.in_(['PENDING','UNKNOWN'])))); db.close()"
    return int(compose("exec", "-T", "m1", "uv", "run", "--no-sync", "python", "-c", code).strip())


def test_d_outage_recovers_original_notification_and_m1_restart_preserves_it(compose):
    compose("restart", "m1"); ready("m1")
    _, seat = private_room()
    owner = admin = login("admin")
    reservation = call("m1", "reserve_seat", {"seatId": seat, **slot(), "requestId": rid()}, owner)
    key = "m1-before-" + reservation["reservationId"]
    advance(6)
    def drained(count):
        assert count == 0
    eventually(lambda: unconfirmed_m1_outbox(compose), drained, seconds=30)
    try:
        compose("stop", "-t", "1", "d")
        advance(int((parse_time(reservation["startTime"]) - timedelta(minutes=15) - utc_now()).total_seconds()))
        def queued(status):
            assert status == "UNKNOWN"
        eventually(lambda: outbox_status(compose, key), queued, seconds=35)
    finally:
        compose("start", "d"); ready("d")
    # worker的补偿退避使用受控时间，不能只等待真实时间。
    advance(6)
    def delivered(response):
        assert response.status_code == 200
        assert all(d["status"] == "SUBMITTED" and d["attemptCount"] == 1 for d in response.json()["data"]["deliveries"])
    original = eventually(lambda: post("d", "get_notification", {"requestId": key}, admin), delivered, seconds=30).json()["data"]
    payload = {k: original[k] for k in ("requestId", "sourceModule", "businessId", "spaceId", "kind", "title", "body")}
    payload["recipientUserIds"] = [d["recipientUserId"] for d in original["deliveries"]]
    replayed = call("d", "send_notification", payload, source="m1")
    assert replayed["notificationId"] == original["notificationId"]
    compose("restart", "m1"); ready("m1")
    assert outbox_status(compose, key) == "SENT"
    assert call("m1", "get_my_reservation", {"reservationId": reservation["reservationId"]}, owner)["reservationId"] == reservation["reservationId"]
    unchanged = call("d", "get_notification", {"requestId": key}, admin)
    assert unchanged["notificationId"] == original["notificationId"]
    assert all(d["attemptCount"] == 1 for d in unchanged["deliveries"])


def test_database_outage_exposes_worker_error_and_recovers_persisted_state(compose):
    _, seat = private_room()
    student = login("student2")
    reservation = call("m1", "reserve_seat", {"seatId": seat, **slot(5), "requestId": rid()}, student)
    try:
        compose("stop", "-t", "1", "postgres")
        def failed(response):
            assert response.status_code == 200 and response.json()["data"]["workerError"]
        eventually(lambda: httpx.get(Settings.load("m2").peer_urls["m2"] + "/health", trust_env=False, timeout=3), failed, seconds=20)
    finally:
        compose("start", "postgres")
        for module in ("d", "m1", "m2", "m3"):
            ready(module, seconds=45)
    restored = call("m1", "get_my_reservation", {"reservationId": reservation["reservationId"]}, student)
    assert restored["reservationId"] == reservation["reservationId"] and restored["status"] == "RESERVED"
