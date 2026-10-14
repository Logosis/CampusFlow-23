import asyncio
import io
import json
from pathlib import Path
from datetime import datetime, timezone
import httpx
import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from alembic import command
from alembic.config import Config
from backend.shared.client import Actor, call_peer
from backend.shared.errors import ApiError, success, STATUS
from backend.shared.clock import parse_time, format_time, utc_now
from backend.shared.factory import create_app
from backend.shared.models import InputModel, Id, PositiveInt, IsoTime
from backend.shared.settings import Settings
from backend.shared.registry import SERVICES

ROOT = Path(__file__).resolve().parents[2]

def settings(module="m1"):
    return Settings(module, "test", "", {m: f"http://{m}" for m in SERVICES},
                    {m: f"secret-{m}" for m in SERVICES}, 5)

def test_time_requires_timezone_and_outputs_utc():
    assert format_time(parse_time("2026-01-01T08:00:00+08:00")) == "2026-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        parse_time("2026-01-01T08:00:00")

def test_controlled_clock_only_works_in_test(tmp_path, monkeypatch):
    path = tmp_path / "now.txt"
    path.write_text("2026-01-01T00:00:00Z", encoding="utf-8")
    monkeypatch.setenv("TEST_CLOCK_FILE", str(path))
    monkeypatch.setenv("APP_ENV", "test")
    assert utc_now() == datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setenv("APP_ENV", "production")
    assert abs((utc_now() - datetime.now(timezone.utc)).total_seconds()) < 5

def test_inputs_are_strict_and_reject_unknown_fields():
    class Input(InputModel):
        requestId: Id
        expectedVersion: PositiveInt
        startTime: IsoTime
    valid = dict(requestId="r1", expectedVersion=1, startTime="2026-01-01T00:00:00Z")
    Input(**valid)
    for changes in [{"expectedVersion": True}, {"expectedVersion": "1"}, {"requestId": ""},
                    {"surprise": 1}, {"startTime": "2026-01-01T00:00:00"}]:
        with pytest.raises(ValidationError):
            Input(**(valid | changes))

def test_every_registered_business_placeholder_fails_instead_of_fake_success():
    for module, funcs in SERVICES.items():
        app = create_app(module, APIRouter())
        with TestClient(app) as client:
            health = client.get("/health").json()["data"]
            assert set(health["unfinishedFunctions"]) == {n for n, f in funcs.items() if f["required"]}
            assert health["workerConfigured"] is False
            for function in funcs:
                result = client.post(f"/api/v1/{module}/{function}", json={})
                assert result.status_code == 503
                assert result.json()["ok"] is False
                assert result.json()["data"] is None

def test_router_rejects_unknown_and_duplicate_paths():
    for paths in [("/invented",), ("/login", "/login")]:
        router = APIRouter()
        for path in paths:
            router.add_api_route(path, lambda: success({}), methods=["POST"])
        with pytest.raises(ValueError):
            create_app("d", router)

def test_newly_required_frontend_routes_register_and_still_require_identity(monkeypatch):
    # 仅验证共同路由与身份门槛；端点和D身份返回是测试替身，不代表业务完成。
    async def identity_call(config, module, function, payload, actor):
        assert (module, function, payload) == ("d", "get_identity", {})
        assert actor.mode == "USER"
        return {"userId": "u1", "accountStatus": "ACTIVE"}
    monkeypatch.setattr("backend.shared.auth.call_peer", identity_call)
    functions = {
        "m2": ["get_applications", "cancel_application", "get_maintenance_impact",
               "get_approval_config", "update_approval_config", "get_classroom_options"],
        "m3": ["get_work_orders", "get_work_order", "cancel_work_order", "get_maintenance_stats"],
    }
    for module, names in functions.items():
        router = APIRouter()
        async def endpoint(request: Request):
            return success({"userId": request.state.identity["userId"]})
        for name in names:
            assert SERVICES[module][name]["required"] is True
            router.add_api_route(f"/{name}", endpoint, methods=["POST"])
        app = create_app(module, router)
        app.state.settings = settings(module)
        with TestClient(app) as client:
            missing = client.get("/health").json()["data"]["unfinishedFunctions"]
            assert not set(names).intersection(missing)
            for name in names:
                path = f"/api/v1/{module}/{name}"
                assert client.post(path, json={}).status_code == 401
                headers = {"Authorization": "Bearer user"}
                assert client.post(path, json={}, headers=headers).json()["data"] == {"userId": "u1"}
                assert client.post(path, json={}, headers=headers | {"X-Actor-Mode": "SYSTEM"}).status_code == 422
                assert client.post(path, json={}, headers={"X-Service-Credential": "secret-d", "X-Actor-Mode": "SYSTEM"}).status_code == 403

def test_validation_unknown_route_and_exceptions_have_common_envelope():
    class Login(InputModel):
        account: Id
    router = APIRouter()
    @router.post("/login")
    async def login(input: Login):
        raise RuntimeError("internal secret must not appear")
    app = create_app("d", router)
    with TestClient(app, raise_server_exceptions=False) as client:
        for path, body, code in [("/api/v1/d/login", {"account": "a", "extra": 1}, "INVALID_ARGUMENT"),
                                 ("/unknown", {}, "NOT_FOUND"),
                                 ("/api/v1/d/login", {"account": "a"}, "INTERNAL_ERROR")]:
            response = client.post(path, json=body)
            assert response.status_code == STATUS[code]
            assert response.json()["error"]["code"] == code
            assert "internal secret" not in response.text

def test_invalid_module_credential_cannot_fall_back_to_public_login():
    router = APIRouter()
    @router.post("/login")
    async def login():
        return success({})
    app = create_app("d", router)
    app.state.settings = settings("d")
    with TestClient(app) as client:
        result = client.post("/api/v1/d/login", json={}, headers={"X-Service-Credential": "forged"})
        assert result.status_code == 401

def test_user_identity_is_required_but_repeated_logout_can_use_revoked_token():
    router = APIRouter()
    @router.post("/get_identity")
    async def get_identity(request: Request):
        return success(request.state.identity)
    @router.post("/logout")
    async def logout():
        return success({"loggedOut": True})
    app = create_app("d", router)
    app.state.settings = settings("d")
    async def resolver(token):
        if token != "valid":
            raise ApiError("UNAUTHENTICATED", "无效")
        return {"userId": "u1", "accountStatus": "ACTIVE"}
    app.state.identity_resolver = resolver
    with TestClient(app) as client:
        assert client.post("/api/v1/d/get_identity", json={}).status_code == 401
        assert client.post("/api/v1/d/get_identity", json={}, headers={"Authorization": "Bearer revoked"}).status_code == 401
        assert client.post("/api/v1/d/get_identity", json={}, headers={"Authorization": "Bearer valid"}).status_code == 200
        assert client.post("/api/v1/d/logout", json={}, headers={"Authorization": "Bearer revoked"}).status_code == 200

def test_direct_audit_can_query_but_cannot_append_module_events():
    router = APIRouter()
    @router.post("/audit")
    async def audit(input: dict):
        return success({})
    app = create_app("d", router)
    app.state.settings = settings("d")
    async def resolver(token):
        return {"userId": "u1", "accountStatus": "ACTIVE"}
    app.state.identity_resolver = resolver
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer user"}
        assert client.post("/api/v1/d/audit", json={"operation": "LIST"}, headers=headers).status_code == 200
        assert client.post("/api/v1/d/audit", json={"operation": "APPEND"}, headers=headers).status_code == 403

def test_system_calls_cannot_impersonate_user_or_call_wrong_provider():
    router = APIRouter()
    @router.post("/change_room_limit")
    async def change_room_limit(request: Request):
        return success({"source": request.state.source_module})
    app = create_app("m1", router)
    app.state.settings = settings()
    with TestClient(app) as client:
        headers = {"X-Service-Credential": "secret-m2", "X-Actor-Mode": "SYSTEM"}
        assert client.post("/api/v1/m1/change_room_limit", json={}, headers=headers).status_code == 200
        assert client.post("/api/v1/m1/change_room_limit", json={}, headers=headers | {"Authorization": "Bearer u"}).status_code == 422
        assert client.post("/api/v1/m1/change_room_limit", json={}, headers=headers | {"X-Service-Credential": "secret-d"}).status_code == 403
        assert client.post("/api/v1/m1/change_room_limit", json={}).status_code == 403

def test_peer_client_forwards_identity_and_original_input():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=success({"value": 1}))
    result = asyncio.run(call_peer(settings(), "d", "get_identity", {"requestId": "unchanged"},
                                  Actor("USER", "user-token"), transport=httpx.MockTransport(handle)))
    assert result == {"value": 1}
    assert seen[0].headers["X-Service-Credential"] == "secret-m1"
    assert seen[0].headers["Authorization"] == "Bearer user-token"
    assert json.loads(seen[0].content)["requestId"] == "unchanged"

@pytest.mark.parametrize("kind", ["timeout", "server", "bad_envelope"])
def test_peer_uncertainty_is_not_success_and_is_not_automatically_retried(kind):
    seen = []
    def handle(request):
        seen.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if kind == "server":
            return httpx.Response(500, json={"ok": False, "data": None, "error": {"code": "INTERNAL_ERROR", "message": "fail"}})
        return httpx.Response(200, json={"items": []})
    with pytest.raises(ApiError) as error:
        asyncio.run(call_peer(settings(), "d", "send_notification", {"requestId": "r"},
                              Actor("SYSTEM"), transport=httpx.MockTransport(handle)))
    assert error.value.code == "UNAVAILABLE"
    assert len(seen) == 1

def test_peer_explicit_rejection_is_preserved():
    def handle(request):
        return httpx.Response(409, json={"ok": False, "data": None,
                                        "error": {"code": "CONFLICT", "message": "已占用"}})
    with pytest.raises(ApiError) as error:
        asyncio.run(call_peer(settings(), "d", "query_master", {}, Actor("SYSTEM"),
                              transport=httpx.MockTransport(handle)))
    assert error.value.code == "CONFLICT"

def test_each_migration_stream_can_replay_offline(monkeypatch):
    for module in SERVICES:
        monkeypatch.setenv(f"{module.upper()}_DATABASE_URL", "postgresql+psycopg://offline:offline@localhost/offline")
        output = io.StringIO()
        config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
        config.set_main_option("script_location", str(ROOT / "backend" / module / "migrations"))
        command.upgrade(config, "head", sql=True)
        sql = output.getvalue()
        assert "alembic_version" in sql and f"{module}_0001" in sql
        assert "DROP DATABASE" not in sql

def test_registry_matches_function_names_in_module_contracts():
    import re
    for module, funcs in SERVICES.items():
        name = "Module_D/D.md" if module == "d" else f"Module_{module.upper()}/{module.upper()}.md"
        text = (ROOT / "docs/planning/R1" / name).read_text(encoding="utf-8")
        listed = set(re.findall(r"\b([a-z][a-z_]+)\(input\)", text))
        listed.discard("fn")  # 文档的通用函数写法，不是公开服务。
        assert set(funcs) == listed
        assert all(f["serviceId"].startswith(module.upper() + "-S") for f in funcs.values())
