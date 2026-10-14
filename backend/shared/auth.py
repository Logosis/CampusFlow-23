from secrets import compare_digest
from fastapi import Request
from backend.shared.client import Actor, call_peer
from backend.shared.errors import ApiError

def service_identity(token: str | None, settings) -> str | None:
    if token is None:
        return None
    for name, value in settings.credentials.items():
        if value and compare_digest(token, value):
            return name
    raise ApiError("UNAUTHENTICATED", "模块凭据无效")

def bearer(request: Request) -> str | None:
    value = request.headers.get("Authorization")
    if value is None:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError("UNAUTHENTICATED", "用户凭据格式不合法")
    return token

async def current_user(request: Request) -> dict:
    token = bearer(request)
    if not token:
        raise ApiError("UNAUTHENTICATED", "需要用户登录")
    settings = request.app.state.settings
    if settings.module == "d":
        resolver = getattr(request.app.state, "identity_resolver", None)
        if resolver is None:
            raise ApiError("UNAVAILABLE", "D身份验证尚未实现")
        identity = await resolver(token)
    else:
        identity = await call_peer(settings, "d", "get_identity", {}, Actor("USER", token))
    if not isinstance(identity, dict) or not isinstance(identity.get("userId"), str):
        raise ApiError("UNAVAILABLE", "身份返回格式不符合约定")
    if identity.get("accountStatus") != "ACTIVE":
        raise ApiError("UNAUTHENTICATED", "账号不可登录")
    return identity
