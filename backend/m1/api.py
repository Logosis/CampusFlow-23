from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, time, timedelta, timezone
from hmac import compare_digest
from uuid import uuid4

from fastapi import APIRouter, Request
from sqlalchemy import and_, select, text
from sqlalchemy.orm import Session

from backend.m1.models import (
    IdempotencyRecord,
    CheckInCode,
    LimitAggregate,
    LimitRequest,
    M1Config,
    ManagementLock,
    OpeningRange,
    OpeningSchedule,
    Reservation,
)
from backend.m1.outbox import audit as queue_audit
from backend.m1.outbox import notification as queue_notification
from backend.m1.schemas import (
    ChangeRoomLimitInput,
    CheckInCodeOut,
    CheckInInput,
    GetCheckInCodeInput,
    GetConfigInput,
    GetConfigOut,
    GetMyReservationInput,
    GetMyReservationsInput,
    GetOpeningTimesInput,
    GetRoomAvailabilityInput,
    GetRoomLimitInput,
    GetUsageInput,
    Page,
    ProxyInput,
    ProxyOutput,
    ReservationOut,
    ReserveSeatInput,
    RoomAvailabilityOut,
    RoomLimitOut,
    SearchSeatItem,
    SearchSeatsInput,
    SetOpeningTimesInput,
    OpeningTimesOut,
    UpdateConfigInput,
    UsageOut,
)
from backend.shared.auth import bearer
from backend.shared.client import Actor, call_peer
from backend.shared.clock import CAMPUS_TZ, format_time, parse_time, utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError, success
from backend.shared.models import SuccessResponse


router = APIRouter()
ACTIVE_RESERVATION_STATUSES = ("RESERVED", "CHECKED_IN")


def _session() -> Session:
    return session_factory("m1")()


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reservation_out(row: Reservation) -> dict:
    return {
        "reservationId": row.reservation_id,
        "userId": row.user_id,
        "spaceId": row.space_id,
        "seatId": row.seat_id,
        "startTime": format_time(_aware(row.start_time)),
        "endTime": format_time(_aware(row.end_time)),
        "status": row.status,
        "checkInTime": format_time(_aware(row.check_in_time)) if row.check_in_time else None,
        "version": row.version,
    }


def _limit_out(row: LimitAggregate) -> dict | None:
    if row.state is None or row.source_version is None:
        return None
    return {
        "source": row.source,
        "businessId": row.business_id,
        "spaceId": row.space_id,
        "startTime": format_time(_aware(row.start_time)) if row.start_time else None,
        "endTime": format_time(_aware(row.end_time)) if row.end_time else None,
        "state": row.state,
        "sourceVersion": row.source_version,
    }


def _room_limit_out(
    row: LimitAggregate | None,
    source: str,
    business_id: str,
    request_id: str | None = None,
    request_row: LimitRequest | None = None,
) -> dict:
    return {
        "source": source,
        "businessId": business_id,
        "spaceId": row.space_id if row else None,
        "maxProcessedVersion": row.max_processed_version if row else None,
        "currentLimit": _limit_out(row) if row else None,
        "queriedRequestId": request_id,
        "requestStatus": request_row.status if request_row else ("UNKNOWN" if request_id else None),
        "message": request_row.message if request_row else ("未找到该请求的处理记录" if request_id else ""),
    }


def _lock(db: Session, key: str) -> None:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"m1:{key}"})


def _config(db: Session) -> M1Config:
    row = db.get(M1Config, "default")
    if row is None:
        row = M1Config(key="default", max_reservation_hours=4, version=1)
        db.add(row)
        db.flush()
    return row


def _validate_period(start: datetime, end: datetime, *, whole_hours: bool, max_hours: int | None = None) -> None:
    if start >= end:
        raise ApiError("INVALID_ARGUMENT", "开始时间必须早于结束时间")
    if whole_hours and any((start.minute, start.second, start.microsecond, end.minute, end.second, end.microsecond)):
        raise ApiError("INVALID_ARGUMENT", "个人预约必须整点开始和结束")
    seconds = (end - start).total_seconds()
    if whole_hours and (seconds < 3600 or seconds % 3600 != 0):
        raise ApiError("INVALID_ARGUMENT", "个人预约须为完整小时")
    if max_hours is not None and seconds > max_hours * 3600:
        raise ApiError("INVALID_ARGUMENT", "预约时长超过当前上限")


def _opening_contains(db: Session, space_id: str, start: datetime, end: datetime) -> bool:
    return db.scalar(
        select(OpeningRange.id).where(
            OpeningRange.space_id == space_id,
            OpeningRange.start_time <= start,
            OpeningRange.end_time >= end,
        ).limit(1)
    ) is not None


def _overlap(start_column, end_column, start: datetime, end: datetime):
    return and_(start_column < end, end_column > start)


def _encode_cursor(offset: int, signature: str) -> str:
    raw = json.dumps({"offset": offset, "signature": signature}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None, signature: str) -> int:
    if cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        if set(data) != {"offset", "signature"} or data["signature"] != signature:
            raise ValueError
        offset = data["offset"]
        if type(offset) is not int or offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ApiError("INVALID_ARGUMENT", "分页游标无效或与当前筛选条件不匹配")


def _page(items: list[dict], limit: int, cursor: str | None, signature_payload: dict) -> dict:
    signature = _hash(signature_payload)
    offset = _decode_cursor(cursor, signature)
    if offset > len(items):
        raise ApiError("INVALID_ARGUMENT", "分页游标超出结果范围")
    selected = items[offset:offset + limit]
    next_offset = offset + len(selected)
    return {
        "items": selected,
        "nextCursor": _encode_cursor(next_offset, signature) if next_offset < len(items) else None,
    }


async def _peer(request: Request, target: str, function: str, payload: dict, mode: str) -> dict:
    token = bearer(request) if mode == "USER" else None
    return await call_peer(request.app.state.settings, target, function, payload, Actor(mode, token))


async def _permission(request: Request, code: str, space_id: str | None = None) -> dict:
    payload: dict = {"permissionCode": code}
    if space_id is not None:
        payload["targetScope"] = {"type": "SPACE", "id": space_id}
    result = await _peer(request, "d", "check_permission", payload, "USER")
    if result.get("allowed") is not True:
        raise ApiError("FORBIDDEN", result.get("reason") or "没有执行该操作的权限")
    return result


async def _allowed(request: Request, code: str, space_id: str) -> bool:
    result = await _peer(request, "d", "check_permission", {
        "permissionCode": code,
        "targetScope": {"type": "SPACE", "id": space_id},
    }, "USER")
    return result.get("allowed") is True


async def _master(request: Request, operation: str, values: dict, mode: str = "USER") -> dict:
    return await _peer(request, "d", "query_master", {"operation": operation, **values}, mode)


async def _all_master(request: Request, operation: str, values: dict, mode: str) -> list[dict]:
    items: list[dict] = []
    cursor = None
    for _ in range(1000):
        payload = {**values, "limit": 100}
        if cursor is not None:
            payload["cursor"] = cursor
        page = await _master(request, operation, payload, mode)
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise ApiError("UNAVAILABLE", "D主数据返回格式不符合约定")
        items.extend(page["items"])
        cursor = page.get("nextCursor")
        if cursor is None:
            return items
    raise ApiError("UNAVAILABLE", "D主数据分页未能结束")


def _check_study_access(identity: dict, space: dict) -> None:
    if not space.get("registered") or not space.get("enabled") or not space.get("isStudyRoom"):
        raise ApiError("CONFLICT", "教室当前不可用于个人预约")
    if space.get("ownerKind") == "DEPARTMENT" and identity.get("departmentId") != space.get("departmentId"):
        raise ApiError("FORBIDDEN", "该自习室仅对所属院系开放")


def _local_availability(
    db: Session,
    space_id: str,
    start: datetime,
    end: datetime,
    *,
    seat_id: str | None = None,
    user_id: str | None = None,
    exclude_limit: tuple[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    blocked: list[str] = []
    reasons: list[str] = []
    reservations = select(Reservation.reservation_id).where(
        Reservation.status.in_(ACTIVE_RESERVATION_STATUSES),
        _overlap(Reservation.start_time, Reservation.end_time, start, end),
    )
    if seat_id is not None:
        reservations = reservations.where(Reservation.seat_id == seat_id)
    else:
        reservations = reservations.where(Reservation.space_id == space_id)
    if db.scalar(reservations.limit(1)) is not None:
        blocked.append("M1")
        reasons.append("目标时段存在个人预约")
    if user_id is not None and db.scalar(
        select(Reservation.reservation_id).where(
            Reservation.user_id == user_id,
            Reservation.status.in_(ACTIVE_RESERVATION_STATUSES),
            _overlap(Reservation.start_time, Reservation.end_time, start, end),
        ).limit(1)
    ) is not None:
        if "M1" not in blocked:
            blocked.append("M1")
            reasons.append("本人已有重叠预约")

    limits = db.scalars(select(LimitAggregate).where(
        LimitAggregate.space_id == space_id,
        LimitAggregate.state == "ACTIVE",
    )).all()
    for item in limits:
        if exclude_limit == (item.source, item.business_id):
            continue
        applies = item.source == "M3" or (
            item.start_time is not None
            and item.end_time is not None
            and _aware(item.start_time) < end
            and _aware(item.end_time) > start
        )
        if applies and item.source not in blocked:
            blocked.append(item.source)
            reasons.append(f"目标时段受{item.source}来源限制")
    return blocked, reasons


def _management_blocked(db: Session, space_id: str, seat_id: str | None = None) -> bool:
    keys = [f"space:{space_id}"]
    if seat_id is not None:
        keys.append(f"seat:{seat_id}")
    return db.scalar(select(ManagementLock.resource_key).where(
        ManagementLock.resource_key.in_(keys)
    ).limit(1)) is not None


def _opening_out(schedule: OpeningSchedule | None, space_id: str) -> dict:
    return {
        "spaceId": space_id,
        "timeRanges": [
            {"startTime": format_time(_aware(item.start_time)), "endTime": format_time(_aware(item.end_time))}
            for item in schedule.ranges
        ] if schedule else [],
        "version": schedule.version if schedule else 1,
    }


def _m1_config_out(row: M1Config) -> dict:
    return {"maxReservationHours": row.max_reservation_hours, "version": row.version}


@router.post("/get_config", response_model=SuccessResponse[GetConfigOut])
async def get_config(input: GetConfigInput, request: Request):
    await _permission(request, "m1.config.read")
    if input.owner == "M1":
        with _session() as db:
            config = _m1_config_out(_config(db))
    else:
        targets = {
            "M2": ("m2", "get_approval_config", {}),
            "M3": ("m3", "get_timeout_config", {}),
            "D": ("d", "manage_public_config", {"operation": "GET"}),
        }
        target, function, payload = targets[input.owner]
        config = await _peer(request, target, function, payload, "USER")
    return success({"owner": input.owner, "config": config})


@router.post("/update_config", response_model=SuccessResponse[GetConfigOut])
async def update_config(input: UpdateConfigInput, request: Request):
    await _permission(request, "m1.config.manage")
    if input.owner != "M1":
        allowed_fields = {
            "M2": {"applicationScopes", "approvalLevels", "clockStart", "timeoutSeconds", "checkIntervalSeconds"},
            "M3": {"checkIntervalSeconds", "rules"},
            "D": {"authMethod", "notificationChannel", "notificationRetryLimit"},
        }
        if set(input.config) != allowed_fields[input.owner]:
            raise ApiError("INVALID_ARGUMENT", "配置字段不符合所属模块契约")
        if input.owner == "M2":
            target, function = "m2", "update_approval_config"
            payload = input.config | {"expectedVersion": input.expectedVersion, "requestId": input.requestId}
        elif input.owner == "M3":
            target, function = "m3", "update_timeout_config"
            payload = input.config | {"expectedVersion": input.expectedVersion, "requestId": input.requestId}
        else:
            target, function = "d", "manage_public_config"
            payload = {"operation": "UPDATE", **input.config,
                       "expectedVersion": input.expectedVersion, "requestId": input.requestId}
        config = await _peer(request, target, function, payload, "USER")
        return success({"owner": input.owner, "config": config})

    if set(input.config) != {"maxReservationHours"} or type(input.config["maxReservationHours"]) is not int:
        raise ApiError("INVALID_ARGUMENT", "M1配置只允许整数maxReservationHours")
    hours = input.config["maxReservationHours"]
    if not 1 <= hours <= 24:
        raise ApiError("INVALID_ARGUMENT", "maxReservationHours范围为1至24")
    identity = request.state.identity
    payload_hash = _hash(input.model_dump(mode="json"))
    now = utc_now()
    with _session() as db:
        _lock(db, "config")
        old = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.function_name == "update_config",
            IdempotencyRecord.actor_key == identity["userId"],
            IdempotencyRecord.request_id == input.requestId,
        ))
        if old:
            if old.input_hash != payload_hash:
                raise ApiError("REQUEST_ID_REUSED", "请求编号已用于不同配置内容")
            return success(old.result)
        row = _config(db)
        if row.version != input.expectedVersion:
            raise ApiError("CONFLICT", "配置版本已变化")
        row.max_reservation_hours = hours
        row.version += 1
        result = {"owner": "M1", "config": _m1_config_out(row)}
        db.add(IdempotencyRecord(
            function_name="update_config", actor_key=identity["userId"], request_id=input.requestId,
            input_hash=payload_hash, result=result, created_at=now,
        ))
        queue_audit(
            db, f"m1-audit-config-{input.requestId}", "USER", identity["userId"], "M1_CONFIG",
            "default", None, "UPDATE", "SUCCESS", "更新M1预约配置", now,
        )
        db.commit()
        return success(result)


@router.post("/manage_roles", response_model=SuccessResponse[ProxyOutput])
async def manage_roles(input: ProxyInput, request: Request):
    return success(await _peer(request, "d", "manage_roles", input.model_dump(mode="json"), "USER"))


@router.post("/query_master", response_model=SuccessResponse[ProxyOutput])
async def query_master(input: ProxyInput, request: Request):
    return success(await _peer(request, "d", "query_master", input.model_dump(mode="json"), "USER"))


def _breaking_master_change(operation: str, current: dict | None, payload: dict) -> bool:
    if operation in {"UNREGISTER_SPACE", "UNREGISTER_SEAT"}:
        return True
    if operation == "UPDATE_SEAT":
        return current is not None and current.get("enabled") and payload.get("enabled") is False
    if operation == "UPDATE_SPACE" and current is not None:
        return (
            (current.get("enabled") and payload.get("enabled") is False)
            or (current.get("isStudyRoom") and payload.get("isStudyRoom") is False)
            or current.get("ownerKind") != payload.get("ownerKind")
            or current.get("departmentId") != payload.get("departmentId")
        )
    return False


@router.post("/maintain_master", response_model=SuccessResponse[ProxyOutput])
async def maintain_master(input: ProxyInput, request: Request):
    input = input.model_dump(mode="json")
    operation = input.get("operation")
    if operation not in {"CREATE_SPACE", "UPDATE_SPACE", "UNREGISTER_SPACE", "CREATE_SEAT", "UPDATE_SEAT", "UNREGISTER_SEAT"}:
        # D remains the canonical validator and returns the common error envelope.
        return success(await _peer(request, "d", "maintain_master", input, "USER"))
    request_id = input.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        raise ApiError("INVALID_ARGUMENT", "主数据变更缺少requestId")
    current = None
    space_id = input.get("spaceId")
    seat_id = input.get("seatId")
    if operation in {"UPDATE_SPACE", "UNREGISTER_SPACE"}:
        current = await _master(request, "GET_SPACE", {"spaceId": space_id}, "USER")
    elif operation in {"UPDATE_SEAT", "UNREGISTER_SEAT"}:
        current = await _master(request, "GET_SEAT", {"seatId": seat_id}, "USER")
        space_id = current["spaceId"]
    elif operation == "CREATE_SEAT":
        space_id = input.get("spaceId")
    lock_keys = [f"space:{space_id}"] if isinstance(space_id, str) else []
    if isinstance(seat_id, str):
        lock_keys.append(f"seat:{seat_id}")
    if _breaking_master_change(operation, current, input):
        with _session() as db:
            query = select(Reservation.reservation_id).where(Reservation.status.in_(ACTIVE_RESERVATION_STATUSES))
            query = query.where(Reservation.seat_id == seat_id) if seat_id else query.where(Reservation.space_id == space_id)
            if db.scalar(query.limit(1)) is not None:
                raise ApiError("CONFLICT", "该变更会破坏现有未终结预约")
    if lock_keys:
        with _session() as db:
            for key in sorted(lock_keys):
                _lock(db, key)
            existing = db.scalars(select(ManagementLock).where(
                ManagementLock.resource_key.in_(lock_keys)
            )).all()
            if any(item.request_id != request_id for item in existing):
                raise ApiError("CONFLICT", "相关主数据正在维护")
            for key in lock_keys:
                if not any(item.resource_key == key for item in existing):
                    db.add(ManagementLock(resource_key=key, request_id=request_id, created_at=utc_now()))
            if _breaking_master_change(operation, current, input):
                query = select(Reservation.reservation_id).where(Reservation.status.in_(ACTIVE_RESERVATION_STATUSES))
                query = query.where(Reservation.seat_id == seat_id) if seat_id else query.where(Reservation.space_id == space_id)
                if db.scalar(query.limit(1)) is not None:
                    raise ApiError("CONFLICT", "该变更会破坏现有未终结预约")
            db.commit()
    try:
        result = await _peer(request, "d", "maintain_master", input, "USER")
    except ApiError as exc:
        if exc.code != "UNAVAILABLE" and lock_keys:
            with _session() as db:
                rows = db.scalars(select(ManagementLock).where(
                    ManagementLock.resource_key.in_(lock_keys), ManagementLock.request_id == request_id
                )).all()
                for row in rows:
                    db.delete(row)
                db.commit()
        raise
    if lock_keys:
        with _session() as db:
            rows = db.scalars(select(ManagementLock).where(
                ManagementLock.resource_key.in_(lock_keys), ManagementLock.request_id == request_id
            )).all()
            for row in rows:
                db.delete(row)
            db.commit()
    return success(result)


@router.post("/get_opening_times", response_model=SuccessResponse[OpeningTimesOut])
async def get_opening_times(input: GetOpeningTimesInput, request: Request):
    await _permission(request, "m1.opening.read", input.spaceId)
    await _master(request, "GET_SPACE", {"spaceId": input.spaceId}, "USER")
    with _session() as db:
        return success(_opening_out(db.get(OpeningSchedule, input.spaceId), input.spaceId))


@router.post("/set_opening_times", response_model=SuccessResponse[OpeningTimesOut])
async def set_opening_times(input: SetOpeningTimesInput, request: Request):
    await _permission(request, "m1.opening.manage", input.spaceId)
    await _master(request, "GET_SPACE", {"spaceId": input.spaceId}, "USER")
    ranges = [(parse_time(item.startTime), parse_time(item.endTime)) for item in input.timeRanges]
    for start, end in ranges:
        _validate_period(start, end, whole_hours=False)
    ordered = sorted(ranges)
    if ranges != ordered or any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        raise ApiError("INVALID_ARGUMENT", "开放区间必须按时间排序且不得重叠")
    identity = request.state.identity
    payload_hash = _hash(input.model_dump(mode="json"))
    now = utc_now()
    lock_key = f"space:{input.spaceId}"
    with _session() as db:
        _lock(db, lock_key)
        old = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.function_name == "set_opening_times",
            IdempotencyRecord.actor_key == identity["userId"],
            IdempotencyRecord.request_id == input.requestId,
        ))
        if old:
            if old.input_hash != payload_hash:
                raise ApiError("REQUEST_ID_REUSED", "请求编号已用于不同开放时间")
            return success(old.result)
        other_lock = db.get(ManagementLock, lock_key)
        if other_lock and other_lock.request_id != input.requestId:
            raise ApiError("CONFLICT", "该教室正在维护")
        if other_lock is None:
            db.add(ManagementLock(resource_key=lock_key, request_id=input.requestId, created_at=now))
            db.flush()
        schedule = db.get(OpeningSchedule, input.spaceId)
        current_version = schedule.version if schedule else 1
        if current_version != input.expectedVersion:
            raise ApiError("CONFLICT", "开放时间版本已变化")
        active = db.scalars(select(Reservation).where(
            Reservation.space_id == input.spaceId,
            Reservation.status.in_(ACTIVE_RESERVATION_STATUSES),
        )).all()
        for reservation in active:
            reservation_start, reservation_end = _aware(reservation.start_time), _aware(reservation.end_time)
            if not any(start <= reservation_start and end >= reservation_end for start, end in ranges):
                raise ApiError("CONFLICT", "新开放区间未完整包含现有未终结预约")
        if schedule is None:
            schedule = OpeningSchedule(space_id=input.spaceId, version=2)
            db.add(schedule)
        else:
            schedule.version += 1
            schedule.ranges.clear()
        for start, end in ranges:
            schedule.ranges.append(OpeningRange(start_time=start, end_time=end))
        db.flush()
        result = _opening_out(schedule, input.spaceId)
        db.add(IdempotencyRecord(
            function_name="set_opening_times", actor_key=identity["userId"], request_id=input.requestId,
            input_hash=payload_hash, result=result, created_at=now,
        ))
        queue_audit(
            db, f"m1-audit-opening-{input.requestId}", "USER", identity["userId"], "OPENING_SCHEDULE",
            input.spaceId, input.spaceId, "UPDATE", "SUCCESS", "更新教室开放时间", now,
        )
        db.delete(db.get(ManagementLock, lock_key))
        db.commit()
        return success(result)


@router.post("/search_seats", response_model=SuccessResponse[Page[SearchSeatItem]])
async def search_seats(input: SearchSeatsInput, request: Request):
    has_start = input.startTime is not None
    if has_start != (input.endTime is not None):
        raise ApiError("INVALID_ARGUMENT", "搜索时段的开始和结束必须同时提供")
    start = parse_time(input.startTime) if input.startTime else None
    end = parse_time(input.endTime) if input.endTime else None
    with _session() as db:
        if start is not None:
            _validate_period(start, end, whole_hours=True, max_hours=_config(db).max_reservation_hours)
            if start <= utc_now():
                raise ApiError("INVALID_ARGUMENT", "预约开始时间必须晚于当前时间")
    await _permission(request, "m1.seat.read", input.spaceId)
    filters = {"enabled": True, "includeUnregistered": False}
    if input.spaceId is not None:
        filters["spaceId"] = input.spaceId
    if input.socketTag is not None:
        filters["socketTag"] = input.socketTag
    seats = await _all_master(request, "LIST_SEATS", filters, "USER")
    identity = request.state.identity
    spaces: dict[str, dict] = {}
    items: list[dict] = []
    with _session() as db:
        maintenance_spaces = set(db.scalars(select(LimitAggregate.space_id).where(
            LimitAggregate.source == "M3", LimitAggregate.state == "ACTIVE"
        )).all())
        for seat in sorted(seats, key=lambda item: (item.get("spaceId", ""), item.get("seatCode", ""))):
            space_id = seat.get("spaceId")
            if space_id in maintenance_spaces:
                continue
            if space_id not in spaces:
                spaces[space_id] = await _master(request, "GET_SPACE", {"spaceId": space_id}, "USER")
            space = spaces[space_id]
            if not await _allowed(request, "m1.seat.read", space_id):
                continue
            if not space.get("registered") or not space.get("enabled") or not space.get("isStudyRoom"):
                continue
            if space.get("ownerKind") == "DEPARTMENT" and identity.get("departmentId") != space.get("departmentId"):
                continue
            available = None
            reason = "未选择时段"
            if start is not None:
                if _management_blocked(db, space_id, seat["seatId"]):
                    available, reason = False, "相关教室或座位正在维护"
                elif not _opening_contains(db, space_id, start, end):
                    available, reason = False, "目标时段未开放"
                else:
                    blocked, reasons = _local_availability(
                        db, space_id, start, end, seat_id=seat["seatId"], user_id=identity["userId"]
                    )
                    available = not blocked
                    reason = "可预约" if available else "；".join(reasons)
            items.append({"space": space, "seat": seat, "available": available, "reason": reason})
    signature = input.model_dump(mode="json", exclude={"limit", "cursor"})
    return success(_page(items, input.limit, input.cursor, signature))


@router.post("/reserve_seat", response_model=SuccessResponse[ReservationOut])
async def reserve_seat(input: ReserveSeatInput, request: Request):
    identity = request.state.identity
    payload = input.model_dump(mode="json")
    input_hash = _hash(payload)
    with _session() as db:
        previous = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.function_name == "reserve_seat",
            IdempotencyRecord.actor_key == identity["userId"],
            IdempotencyRecord.request_id == input.requestId,
        ))
        if previous:
            if previous.input_hash != input_hash:
                raise ApiError("REQUEST_ID_REUSED", "请求编号已用于不同预约内容")
            previous_result = previous.result
        else:
            previous_result = None
    if previous_result is not None:
        await _permission(request, "m1.reservation.create", previous_result["spaceId"])
        return success(previous_result)
    seat = await _master(request, "GET_SEAT", {"seatId": input.seatId}, "USER")
    space = await _master(request, "GET_SPACE", {"spaceId": seat["spaceId"]}, "USER")
    await _permission(request, "m1.reservation.create", seat["spaceId"])
    _check_study_access(identity, space)
    if not seat.get("registered") or not seat.get("enabled"):
        raise ApiError("CONFLICT", "座位当前不可预约")
    start, end = parse_time(input.startTime), parse_time(input.endTime)
    now = utc_now()
    with _session() as db:
        config = _config(db)
        _validate_period(start, end, whole_hours=True, max_hours=config.max_reservation_hours)
        if start <= now:
            raise ApiError("INVALID_ARGUMENT", "预约开始时间必须晚于当前时间")
        for key in sorted((f"space:{seat['spaceId']}", f"seat:{input.seatId}", f"user:{identity['userId']}")):
            _lock(db, key)
        old = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.function_name == "reserve_seat",
            IdempotencyRecord.actor_key == identity["userId"],
            IdempotencyRecord.request_id == input.requestId,
        ))
        if old:
            if old.input_hash != input_hash:
                raise ApiError("REQUEST_ID_REUSED", "请求编号已用于不同预约内容")
            return success(old.result)
        if _management_blocked(db, seat["spaceId"], input.seatId):
            raise ApiError("CONFLICT", "相关教室或座位正在维护")
        if not _opening_contains(db, seat["spaceId"], start, end):
            raise ApiError("CONFLICT", "目标时段不在开放区间内")
        blocked, reasons = _local_availability(
            db, seat["spaceId"], start, end, seat_id=input.seatId, user_id=identity["userId"]
        )
        if blocked:
            raise ApiError("CONFLICT", "；".join(reasons))
        reservation = Reservation(
            reservation_id=f"res-{uuid4().hex}",
            user_id=identity["userId"],
            space_id=seat["spaceId"],
            seat_id=input.seatId,
            start_time=start,
            end_time=end,
            status="RESERVED",
            check_in_time=None,
            version=1,
            created_at=now,
        )
        db.add(reservation)
        db.flush()
        result = _reservation_out(reservation)
        db.add(IdempotencyRecord(
            function_name="reserve_seat", actor_key=identity["userId"], request_id=input.requestId,
            input_hash=input_hash, result=result, created_at=now,
        ))
        queue_audit(
            db, f"m1-audit-reserve-{input.requestId}", "USER", identity["userId"], "RESERVATION",
            reservation.reservation_id, reservation.space_id, "RESERVE", "SUCCESS", "创建个人预约", now,
        )
        db.commit()
        return success(result)


@router.post("/get_check_in_code", response_model=SuccessResponse[CheckInCodeOut])
async def get_check_in_code(input: GetCheckInCodeInput, request: Request):
    await _permission(request, "m1.check_in_code.read", input.spaceId)
    space = await _master(request, "GET_SPACE", {"spaceId": input.spaceId}, "USER")
    if not space.get("registered") or not space.get("isStudyRoom"):
        raise ApiError("CONFLICT", "目标不是登记中的自习教室")
    now = utc_now()
    campus_now = now.astimezone(CAMPUS_TZ)
    campus_date = campus_now.date().isoformat()
    next_date = campus_now.date() + timedelta(days=1)
    expires_at = datetime.combine(next_date, time.min, CAMPUS_TZ).astimezone(timezone.utc)
    with _session() as db:
        _lock(db, f"check-in-code:{input.spaceId}:{campus_date}")
        row = db.scalar(select(CheckInCode).where(
            CheckInCode.space_id == input.spaceId,
            CheckInCode.campus_date == campus_date,
        ))
        if row is None:
            token = secrets.token_urlsafe(24)
            row = CheckInCode(
                space_id=input.spaceId,
                campus_date=campus_date,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                token=token,
                expires_at=expires_at,
            )
            db.add(row)
            db.commit()
        return success({
            "spaceId": input.spaceId,
            "date": campus_date,
            "token": row.token,
            "expiresAt": format_time(_aware(row.expires_at)),
        })


@router.post("/check_in", response_model=SuccessResponse[ReservationOut])
async def check_in(input: CheckInInput, request: Request):
    identity = request.state.identity
    payload_hash = _hash(input.model_dump(mode="json"))
    now = utc_now()
    with _session() as db:
        reservation = db.get(Reservation, input.reservationId)
        if reservation is None or reservation.user_id != identity["userId"]:
            raise ApiError("NOT_FOUND", "预约不存在")
        space_id = reservation.space_id
    await _permission(request, "m1.reservation.check_in", space_id)
    campus_date = now.astimezone(CAMPUS_TZ).date().isoformat()
    with _session() as db:
        _lock(db, f"reservation:{input.reservationId}")
        reservation = db.scalar(select(Reservation).where(
            Reservation.reservation_id == input.reservationId
        ).with_for_update())
        old = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.function_name == "check_in",
            IdempotencyRecord.actor_key == identity["userId"],
            IdempotencyRecord.request_id == input.requestId,
        ))
        if old:
            if old.input_hash != payload_hash:
                raise ApiError("REQUEST_ID_REUSED", "请求编号已用于不同签到内容")
            return success(old.result)
        if reservation is None or reservation.user_id != identity["userId"]:
            raise ApiError("NOT_FOUND", "预约不存在")
        if reservation.status != "RESERVED":
            raise ApiError("CONFLICT", "当前预约状态不能签到")
        if reservation.version != input.expectedVersion:
            raise ApiError("CONFLICT", "预约版本已变化")
        start = _aware(reservation.start_time)
        if now < start or now >= start + timedelta(minutes=15):
            raise ApiError("CONFLICT", "当前不在签到窗口内")
        code = db.scalar(select(CheckInCode).where(
            CheckInCode.space_id == reservation.space_id,
            CheckInCode.campus_date == campus_date,
        ))
        supplied_hash = hashlib.sha256(input.token.encode()).hexdigest()
        if code is None or _aware(code.expires_at) <= now or not compare_digest(code.token_hash, supplied_hash):
            raise ApiError("CONFLICT", "签到凭据无效或已过期")
        reservation.status = "CHECKED_IN"
        reservation.check_in_time = now
        reservation.version += 1
        result = _reservation_out(reservation)
        db.add(IdempotencyRecord(
            function_name="check_in", actor_key=identity["userId"], request_id=input.requestId,
            input_hash=payload_hash, result=result, created_at=now,
        ))
        queue_audit(
            db, f"m1-audit-checkin-{input.requestId}", "USER", identity["userId"], "RESERVATION",
            reservation.reservation_id, reservation.space_id, "CHECK_IN", "SUCCESS", "预约签到", now,
        )
        db.commit()
        return success(result)


@router.post("/get_my_reservations", response_model=SuccessResponse[Page[ReservationOut]])
async def get_my_reservations(input: GetMyReservationsInput, request: Request):
    await _permission(request, "m1.reservation.read")
    has_start = input.startTime is not None
    if has_start != (input.endTime is not None):
        raise ApiError("INVALID_ARGUMENT", "筛选时段的开始和结束必须同时提供")
    start = parse_time(input.startTime) if input.startTime else None
    end = parse_time(input.endTime) if input.endTime else None
    if start is not None and start >= end:
        raise ApiError("INVALID_ARGUMENT", "开始时间必须早于结束时间")
    with _session() as db:
        query = select(Reservation).where(Reservation.user_id == request.state.identity["userId"])
        if input.status:
            query = query.where(Reservation.status == input.status)
        if start is not None:
            query = query.where(Reservation.start_time >= start, Reservation.start_time < end)
        rows = db.scalars(query.order_by(Reservation.start_time, Reservation.reservation_id)).all()
        detached = [_reservation_out(row) for row in rows]
    allowed_spaces = {
        space_id: await _allowed(request, "m1.reservation.read", space_id)
        for space_id in sorted({item["spaceId"] for item in detached})
    }
    items = [item for item in detached if allowed_spaces[item["spaceId"]]]
    signature = input.model_dump(mode="json", exclude={"limit", "cursor"})
    return success(_page(items, input.limit, input.cursor, signature))


@router.post("/get_my_reservation", response_model=SuccessResponse[ReservationOut])
async def get_my_reservation(input: GetMyReservationInput, request: Request):
    with _session() as db:
        row = db.get(Reservation, input.reservationId)
        if row is None or row.user_id != request.state.identity["userId"]:
            raise ApiError("NOT_FOUND", "预约不存在")
        result = _reservation_out(row)
    await _permission(request, "m1.reservation.read", result["spaceId"])
    return success(result)


def _scope_allows(permission: dict, space: dict) -> bool:
    for scope in permission.get("allowedScopes", []):
        if scope.get("type") == "GLOBAL":
            return True
        if scope.get("type") == "SPACE" and scope.get("id") == space.get("spaceId"):
            return True
        if scope.get("type") == "DEPARTMENT" and scope.get("id") == space.get("departmentId"):
            return True
    return False


@router.post("/get_usage", response_model=SuccessResponse[UsageOut])
async def get_usage(input: GetUsageInput, request: Request):
    permission = await _permission(request, "m1.usage.read", input.spaceId)
    start, end = parse_time(input.startTime), parse_time(input.endTime)
    _validate_period(start, end, whole_hours=False)
    if input.seatId is not None:
        seat = await _master(request, "GET_SEAT", {"seatId": input.seatId}, "USER")
        if input.spaceId is not None and seat["spaceId"] != input.spaceId:
            raise ApiError("INVALID_ARGUMENT", "座位不属于指定教室")
    with _session() as db:
        reservation_query = select(Reservation).where(
            Reservation.start_time >= start,
            Reservation.start_time < end,
        )
        if input.spaceId:
            reservation_query = reservation_query.where(Reservation.space_id == input.spaceId)
        if input.seatId:
            reservation_query = reservation_query.where(Reservation.seat_id == input.seatId)
        reservations = list(db.scalars(reservation_query.order_by(
            Reservation.start_time, Reservation.reservation_id
        )).all())
        limit_rows = list(db.scalars(select(LimitAggregate).where(
            LimitAggregate.state == "ACTIVE"
        )).all())
    space_ids = {item.space_id for item in reservations} | {item.space_id for item in limit_rows}
    if input.spaceId:
        space_ids.add(input.spaceId)
    spaces = {space_id: await _master(request, "GET_SPACE", {"spaceId": space_id}, "USER")
              for space_id in sorted(space_ids)}
    if input.spaceId is None:
        reservations = [item for item in reservations if _scope_allows(permission, spaces[item.space_id])]
        limit_rows = [item for item in limit_rows if _scope_allows(permission, spaces[item.space_id])]
    reservation_items = [_reservation_out(item) for item in reservations]
    signature = input.model_dump(mode="json", exclude={"limit", "cursor"})
    reservation_page = _page(reservation_items, input.limit, input.cursor, signature)
    limits: list[dict] = []
    for reservation in reservations:
        if reservation.status in ACTIVE_RESERVATION_STATUSES and _aware(reservation.start_time) < end and _aware(reservation.end_time) > start:
            limits.append({
                "source": "M1", "businessId": reservation.reservation_id, "spaceId": reservation.space_id,
                "seatId": reservation.seat_id, "startTime": format_time(_aware(reservation.start_time)),
                "endTime": format_time(_aware(reservation.end_time)), "state": "ACTIVE", "sourceVersion": None,
            })
    for item in limit_rows:
        applies = item.source == "M3" or (
            item.start_time is not None and item.end_time is not None
            and _aware(item.start_time) < end and _aware(item.end_time) > start
        )
        if applies and (input.spaceId is None or item.space_id == input.spaceId):
            limits.append({
                "source": item.source, "businessId": item.business_id, "spaceId": item.space_id,
                "seatId": None,
                "startTime": format_time(_aware(item.start_time)) if item.start_time else None,
                "endTime": format_time(_aware(item.end_time)) if item.end_time else None,
                "state": "ACTIVE", "sourceVersion": item.source_version,
            })
    limits.sort(key=lambda item: (item["spaceId"], item["source"], item["businessId"]))
    return success({
        "startTime": format_time(start), "endTime": format_time(end), "checkedAt": format_time(utc_now()),
        "reservations": reservation_page, "reservationCount": len(reservations),
        "checkedInCount": sum(item.check_in_time is not None for item in reservations), "limits": limits,
    })


async def _room_availability(
    request: Request,
    space_id: str,
    start: datetime,
    end: datetime,
    exclude_limit: tuple[str, str] | None = None,
) -> dict:
    _validate_period(start, end, whole_hours=False)
    space = await _master(request, "GET_SPACE", {"spaceId": space_id}, "SYSTEM")
    seats = await _all_master(
        request, "LIST_SEATS", {"spaceId": space_id, "enabled": True, "includeUnregistered": False}, "SYSTEM"
    )
    blocked: list[str] = []
    reasons: list[str] = []
    if not space.get("registered") or not space.get("enabled") or not space.get("isStudyRoom"):
        blocked.append("BASE")
        reasons.append("教室基础资料不可用")
    if not seats:
        blocked.append("NO_SEATS")
        reasons.append("教室没有可用登记座位")
    with _session() as db:
        if _management_blocked(db, space_id) and "BASE" not in blocked:
            blocked.append("BASE")
            reasons.append("教室主数据正在维护")
        if not _opening_contains(db, space_id, start, end):
            blocked.append("OPENING")
            reasons.append("目标时段未开放")
        local_blocked, local_reasons = _local_availability(
            db, space_id, start, end, exclude_limit=exclude_limit
        )
        for code, reason in zip(local_blocked, local_reasons):
            if code not in blocked:
                blocked.append(code)
                reasons.append(reason)
    order = ["BASE", "OPENING", "M1", "M2", "M3", "NO_SEATS"]
    paired = sorted(zip(blocked, reasons), key=lambda item: order.index(item[0]))
    return {
        "spaceId": space_id,
        "startTime": format_time(start),
        "endTime": format_time(end),
        "canOccupy": not paired,
        "blockedBy": [item[0] for item in paired],
        "reasons": [item[1] for item in paired],
        "checkedAt": format_time(utc_now()),
    }


@router.post("/get_room_availability", response_model=SuccessResponse[RoomAvailabilityOut])
async def get_room_availability(input: GetRoomAvailabilityInput, request: Request):
    if request.state.source_module != "m2":
        raise ApiError("FORBIDDEN", "仅M2可查询整室占用可用性")
    result = await _room_availability(
        request, input.spaceId, parse_time(input.startTime), parse_time(input.endTime)
    )
    return success(result)


def _validate_limit_input(input: ChangeRoomLimitInput) -> tuple[datetime | None, datetime | None]:
    start = parse_time(input.startTime) if input.startTime is not None else None
    end = parse_time(input.endTime) if input.endTime is not None else None
    if input.source == "M2":
        expected = {"OCCUPY": {"APPROVED"}, "RELEASE": {"REJECTED", "TIMEOUT_CANCELLED", "CANCELLED"}}
        if input.action not in expected or input.businessState not in expected[input.action]:
            raise ApiError("INVALID_ARGUMENT", "M2动作与业务状态不匹配")
        if start is None or end is None:
            raise ApiError("INVALID_ARGUMENT", "M2占用与解除必须携带原申请时段")
        _validate_period(start, end, whole_hours=False)
    else:
        expected = {"REPAIR": {"REPAIRING"}, "RELEASE": {"ACCEPTED"}}
        if input.action not in expected or input.businessState not in expected[input.action]:
            raise ApiError("INVALID_ARGUMENT", "M3动作与业务状态不匹配")
        if start is not None or end is not None:
            raise ApiError("INVALID_ARGUMENT", "M3维修限制不携带时段")
    return start, end


def _add_limit_request(
    db: Session,
    input: ChangeRoomLimitInput,
    input_hash: str,
    status: str,
    message: str,
    now: datetime,
) -> LimitRequest:
    row = LimitRequest(
        source=input.source,
        business_id=input.businessId,
        request_id=input.requestId,
        input_hash=input_hash,
        source_version=input.sourceVersion,
        status=status,
        message=message,
        created_at=now,
    )
    db.add(row)
    return row


@router.post("/change_room_limit", response_model=SuccessResponse[RoomLimitOut])
async def change_room_limit(input: ChangeRoomLimitInput, request: Request):
    caller = (request.state.source_module or "").upper()
    if caller != input.source:
        raise ApiError("FORBIDDEN", "限制来源与可信调用模块不匹配")
    start, end = _validate_limit_input(input)
    payload = input.model_dump(mode="json")
    input_hash = _hash(payload)
    with _session() as db:
        previous = db.scalar(select(LimitRequest).where(
            LimitRequest.source == input.source,
            LimitRequest.request_id == input.requestId,
        ))
        if previous:
            if previous.input_hash != input_hash:
                raise ApiError("REQUEST_ID_REUSED", "请求编号已用于不同限制内容")
            aggregate = db.scalar(select(LimitAggregate).where(
                LimitAggregate.source == input.source,
                LimitAggregate.business_id == input.businessId,
            ))
            previous_result = _room_limit_out(
                aggregate, input.source, input.businessId, input.requestId, previous
            )
            previous_rejection = previous.message if previous.status == "REJECTED" else None
        else:
            previous_result = previous_rejection = None
    if previous_result is not None:
        if previous_rejection is not None:
            raise ApiError("CONFLICT", previous_rejection or "原请求已被拒绝")
        return success(previous_result)
    space = await _master(request, "GET_SPACE", {"spaceId": input.spaceId}, "SYSTEM")
    if not space.get("registered") or not space.get("isStudyRoom"):
        raise ApiError("CONFLICT", "目标不是登记中的自习教室")
    availability = None
    if input.source == "M2" and input.action == "OCCUPY":
        availability = await _room_availability(
            request, input.spaceId, start, end, exclude_limit=(input.source, input.businessId)
        )
    version_hash = _hash(input.model_dump(mode="json", exclude={"requestId"}))
    now = utc_now()
    rejection: str | None = None
    result: dict | None = None
    with _session() as db:
        _lock(db, f"limit:{input.source}:{input.businessId}")
        _lock(db, f"space:{input.spaceId}")
        old_request = db.scalar(select(LimitRequest).where(
            LimitRequest.source == input.source,
            LimitRequest.request_id == input.requestId,
        ))
        if old_request:
            if old_request.input_hash != input_hash:
                raise ApiError("REQUEST_ID_REUSED", "请求编号已用于不同限制内容")
            aggregate = db.scalar(select(LimitAggregate).where(
                LimitAggregate.source == input.source,
                LimitAggregate.business_id == input.businessId,
            ))
            result = _room_limit_out(aggregate, input.source, input.businessId, input.requestId, old_request)
            if old_request.status == "REJECTED":
                rejection = old_request.message or "原请求已被拒绝"
        else:
            aggregate = db.scalar(select(LimitAggregate).where(
                LimitAggregate.source == input.source,
                LimitAggregate.business_id == input.businessId,
            ).with_for_update())
            if aggregate and aggregate.space_id != input.spaceId:
                rejection = "同一业务编号不能更换教室"
            elif aggregate and input.source == "M2" and (
                _aware(aggregate.start_time) != start or _aware(aggregate.end_time) != end
            ):
                rejection = "同一M2业务编号不能更换原申请时段"
            elif aggregate and input.sourceVersion < aggregate.max_processed_version:
                rejection = "来源版本早于已处理版本"
            elif aggregate and input.sourceVersion == aggregate.max_processed_version:
                if aggregate.last_payload_hash != version_hash:
                    rejection = "相同来源版本携带了不同业务内容"
                elif aggregate.last_status == "REJECTED":
                    rejection = aggregate.last_message or "该来源版本已被拒绝"
                else:
                    request_row = _add_limit_request(db, input, input_hash, "SUCCESS", "重复版本已确认", now)
                    result = _room_limit_out(aggregate, input.source, input.businessId, input.requestId, request_row)
            if result is None and rejection is None and input.source == "M2" and input.action == "OCCUPY":
                if availability and not availability["canOccupy"]:
                    rejection = "；".join(availability["reasons"])
                else:
                    blocked, reasons = _local_availability(
                        db, input.spaceId, start, end, exclude_limit=(input.source, input.businessId)
                    )
                    if blocked:
                        rejection = "；".join(reasons)
            if result is None:
                if aggregate is None:
                    aggregate = LimitAggregate(
                        source=input.source,
                        business_id=input.businessId,
                        space_id=input.spaceId,
                        start_time=start,
                        end_time=end,
                        state=None,
                        source_version=None,
                        max_processed_version=input.sourceVersion,
                        last_payload_hash=version_hash,
                        last_status="REJECTED" if rejection else "SUCCESS",
                        last_message=rejection or "",
                        updated_at=now,
                    )
                    db.add(aggregate)
                elif input.sourceVersion > aggregate.max_processed_version:
                    aggregate.max_processed_version = input.sourceVersion
                    aggregate.last_payload_hash = version_hash
                    aggregate.last_status = "REJECTED" if rejection else "SUCCESS"
                    aggregate.last_message = rejection or ""
                    aggregate.updated_at = now
                if rejection is not None:
                    request_row = _add_limit_request(db, input, input_hash, "REJECTED", rejection, now)
                    db.flush()
                    result = _room_limit_out(aggregate, input.source, input.businessId, input.requestId, request_row)
                else:
                    aggregate.space_id = input.spaceId
                    aggregate.start_time = start
                    aggregate.end_time = end
                    aggregate.state = "RELEASED" if input.action == "RELEASE" else "ACTIVE"
                    aggregate.source_version = input.sourceVersion
                    aggregate.max_processed_version = input.sourceVersion
                    aggregate.last_payload_hash = version_hash
                    aggregate.last_status = "SUCCESS"
                    aggregate.last_message = ""
                    aggregate.updated_at = now
                    if input.source == "M3" and input.action == "REPAIR":
                        reservations = db.scalars(select(Reservation).where(
                            Reservation.space_id == input.spaceId,
                            Reservation.status.in_(ACTIVE_RESERVATION_STATUSES),
                        ).with_for_update()).all()
                        for reservation in reservations:
                            reservation.status = "CANCELLED_MAINTENANCE"
                            reservation.version += 1
                            if not reservation.maintenance_cancel_notified:
                                reservation.maintenance_cancel_notified = True
                                event_request_id = f"m1-maintenance-{reservation.reservation_id}-{reservation.version}"
                                queue_notification(
                                    db, event_request_id, reservation.reservation_id, reservation.space_id,
                                    reservation.user_id, "M1_MAINTENANCE_CANCELLED", "预约因教室维修取消",
                                    "教室进入维修，原预约已取消且不会记违约。", now,
                                )
                            queue_audit(
                                db, f"m1-audit-maintenance-{input.requestId}-{reservation.reservation_id}",
                                "SYSTEM", "M1", "RESERVATION", reservation.reservation_id,
                                reservation.space_id, "CANCEL_MAINTENANCE", "SUCCESS",
                                "教室维修取消个人预约且不记违约", now,
                            )
                    request_row = _add_limit_request(db, input, input_hash, "SUCCESS", "", now)
                    queue_audit(
                        db, f"m1-audit-limit-{input.source.lower()}-{input.requestId}", "SYSTEM", "M1",
                        "ROOM_LIMIT", input.businessId, input.spaceId, input.action, "SUCCESS",
                        f"处理{input.source}来源教室限制", now,
                    )
                    db.flush()
                    result = _room_limit_out(aggregate, input.source, input.businessId, input.requestId, request_row)
        db.commit()
    if rejection is not None:
        raise ApiError("CONFLICT", rejection)
    return success(result)


@router.post("/get_room_limit", response_model=SuccessResponse[RoomLimitOut])
async def get_room_limit(input: GetRoomLimitInput, request: Request):
    caller = (request.state.source_module or "").upper()
    if caller != input.source:
        raise ApiError("FORBIDDEN", "只能查询本模块来源的限制")
    with _session() as db:
        aggregate = db.scalar(select(LimitAggregate).where(
            LimitAggregate.source == input.source,
            LimitAggregate.business_id == input.businessId,
        ))
        request_row = None
        if input.requestId is not None:
            request_row = db.scalar(select(LimitRequest).where(
                LimitRequest.source == input.source,
                LimitRequest.business_id == input.businessId,
                LimitRequest.request_id == input.requestId,
            ))
        return success(_room_limit_out(
            aggregate, input.source, input.businessId, input.requestId, request_row
        ))
