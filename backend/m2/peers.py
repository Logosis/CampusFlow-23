"""M2 对其他模块的公开 HTTP 调用。

全部经 backend.shared.client.call_peer；本模块不导入 D/M1/M3 的业务代码、不读写对方数据库。
来源模块由调用模块凭据核验，M2 只声明自己的调用模式。
"""
from __future__ import annotations

from backend.shared.client import Actor, call_peer
from backend.shared.errors import ApiError
from backend.shared.settings import Settings

from backend.m2 import schemas

M2 = "m2"


def _settings() -> Settings:
    return Settings.load("m2")


async def _call(target: str, function: str, payload: dict, actor: Actor) -> dict:
    return await call_peer(_settings(), target, function, payload, actor)


def user_actor(token: str) -> Actor:
    return Actor("USER", token)


def system_actor() -> Actor:
    return Actor("SYSTEM")


# --------------------------------------------------------------------- D 身份与权限
async def check_permission(permission_code: str, target_scope: dict | None, actor: Actor) -> dict:
    """D-S04；权限不存在或调用不合法按公共规则返回错误。"""
    payload: dict = {"permissionCode": permission_code}
    if target_scope is not None:
        payload["targetScope"] = target_scope
    return await _call("d", "check_permission", payload, actor)


async def get_space(space_id: str, actor: Actor) -> dict:
    """D-S06 GET_SPACE。"""
    return await _call("d", "query_master", {"operation": "GET_SPACE", "spaceId": space_id}, actor)


async def list_spaces(filters: dict, actor: Actor) -> dict:
    """D-S06 LIST_SPACES；返回 D 的原始分页。"""
    payload = {"operation": "LIST_SPACES"}
    payload.update({k: v for k, v in filters.items() if v is not None})
    return await _call("d", "query_master", payload, actor)


async def people_by_ids(user_ids: list[str], target_scope: dict, actor: Actor) -> list[dict]:
    """D-S08 BY_IDS，purpose=APPROVAL，核验审批人资格。"""
    data = await _call(
        "d",
        "query_people",
        {
            "mode": "BY_IDS",
            "purpose": "APPROVAL",
            "permissionCodes": ["m2.approval.approve"],
            "targetScope": target_scope,
            "userIds": user_ids,
        },
        actor,
    )
    people = data.get("people") if isinstance(data, dict) else None
    if not isinstance(people, list):
        raise ApiError("UNAVAILABLE", "人员返回格式不符合约定")
    return people


# --------------------------------------------------------------------- M1 整室限制
async def room_availability(space_id: str, start_time: str, end_time: str, actor: Actor) -> dict:
    """M1-S09；结果原样引用。"""
    return await _call(
        "m1",
        "get_room_availability",
        {"spaceId": space_id, "startTime": start_time, "endTime": end_time},
        actor,
    )


async def change_room_limit(payload: dict) -> dict:
    """M1-S10；由 M2 以 SYSTEM 模式写回，来源由模块凭据核验。"""
    return await _call("m1", "change_room_limit", payload, system_actor())


async def room_limit(source: str, business_id: str, request_id: str) -> dict:
    """M1-S11；核实原写回编号。"""
    return await _call(
        "m1", "get_room_limit", {"source": source, "businessId": business_id, "requestId": request_id},
        system_actor(),
    )


# --------------------------------------------------------------------- D 通知与审计
async def send_notification(payload: dict) -> dict:
    return await _call("d", "send_notification", payload, system_actor())


async def audit_append(payload: dict) -> dict:
    return await _call("d", "audit", {"operation": "APPEND", **payload}, system_actor())


def availability_of(data: dict) -> schemas.Availability:
    """按 M1-S09 契约校验并转换；格式不符视为不可用结果，不冒充成功。"""
    try:
        return schemas.Availability(**data)
    except Exception as exc:  # noqa: BLE001 - 统一转成不可用，保留原因
        raise ApiError("UNAVAILABLE", "可用性返回格式不符合约定") from exc
