from dataclasses import dataclass
import httpx
from backend.shared.errors import ApiError, STATUS
from backend.shared.settings import Settings

@dataclass(frozen=True)
class Actor:
    mode: str
    user_credential: str | None = None

async def call_peer(settings: Settings, target: str, function: str, input: dict,
                    actor: Actor, *, transport=None):
    credential = settings.credentials.get(settings.module)
    if not credential:
        raise ApiError("UNAVAILABLE", "模块接入凭据未配置")
    if target not in settings.peer_urls or actor.mode not in {"USER", "SYSTEM"}:
        raise ApiError("INVALID_ARGUMENT", "模块或调用模式不合法")
    if actor.mode == "USER" and not actor.user_credential:
        raise ApiError("UNAUTHENTICATED", "用户凭据缺失")
    if actor.mode == "SYSTEM" and actor.user_credential:
        raise ApiError("INVALID_ARGUMENT", "后台调用不能携带用户凭据")
    headers = {"X-Service-Credential": credential, "X-Actor-Mode": actor.mode}
    if actor.user_credential:
        headers["Authorization"] = f"Bearer {actor.user_credential}"
    try:
        async with httpx.AsyncClient(timeout=settings.peer_timeout, transport=transport, trust_env=False) as client:
            response = await client.post(
                f"{settings.peer_urls[target]}/api/v1/{target}/{function}", json=input, headers=headers)
        body = response.json()
    except (httpx.TransportError, ValueError) as exc:
        raise ApiError("UNAVAILABLE", "对方结果无法确认，保留原请求编号核实") from exc
    if response.status_code >= 500:
        raise ApiError("UNAVAILABLE", "对方结果无法确认，保留原请求编号核实")
    if (not isinstance(body, dict) or set(body) != {"ok", "data", "error"}
            or type(body["ok"]) is not bool):
        raise ApiError("UNAVAILABLE", "对方返回格式不符合约定")
    if body["ok"]:
        if response.status_code != 200 or body["error"] is not None:
            raise ApiError("UNAVAILABLE", "对方返回格式不符合约定")
        return body["data"]
    error = body["error"]
    if (body["data"] is not None or not isinstance(error, dict)
            or set(error) != {"code", "message"} or error.get("code") not in STATUS
            or STATUS[error["code"]] != response.status_code or not isinstance(error["message"], str)):
        raise ApiError("UNAVAILABLE", "对方返回格式不符合约定")
    raise ApiError(error["code"], error["message"])
