"""D-S09/S10：事务受理、按来源幂等及有权限的当前投递状态查询。"""

from email.headerregistry import Address
from email.errors import HeaderParseError
import hashlib
import json
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError

from backend.d.models import Notification, NotificationDelivery, PublicConfig, User
from backend.d.notification_schemas import GetNotificationInput, SendNotificationInput
from backend.d.permissions import (
    _covers, _target_department, check_permission_in_session, check_user_permission_in_session,
)
from backend.shared.clock import format_time, utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError


STORAGE_ERRORS = (InterfaceError, OperationalError, SQLAlchemyTimeoutError)


def _actor(actor_mode, source_module, user_id, *, allow_direct=False):
    if source_module not in {"m1", "m2", "m3"} and not (allow_direct and source_module is None and actor_mode == "USER"):
        raise ApiError("FORBIDDEN", "调用来源没有此通知服务资格")
    if actor_mode not in {"USER", "SYSTEM"}:
        raise ApiError("INVALID_ARGUMENT", "模块调用必须指定USER或SYSTEM")
    if actor_mode == "USER" and (not isinstance(user_id, str) or not user_id.strip()):
        raise ApiError("UNAUTHENTICATED", "需要已核验的用户身份")
    if actor_mode == "SYSTEM" and user_id is not None:
        raise ApiError("INVALID_ARGUMENT", "后台模式不能携带用户身份")


def _scope(space_id):
    return {"type": "GLOBAL", "id": None} if space_id is None else {"type": "SPACE", "id": space_id}


def _mailbox(value):
    """本版只接收单个 ASCII addr-spec，不接收显示名、控制符或国际化本地部分。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "EMAIL_MISSING"
    if not isinstance(value, str) or not value.isascii() or len(value) > 254 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return None, "EMAIL_INVALID"
    try:
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError, IndexError):
        return None, "EMAIL_INVALID"
    if not address.username or not address.domain or len(address.username) > 64 or address.addr_spec != value:
        return None, "EMAIL_INVALID"
    return value, "QUEUED"


def _result(db, row):
    # 不查询邮箱或内部处理令牌，只投影公开的五个投递字段。
    deliveries = db.execute(select(
        NotificationDelivery.recipient_user_id, NotificationDelivery.status,
        NotificationDelivery.attempt_count, NotificationDelivery.last_attempt_at, NotificationDelivery.reason,
    ).where(NotificationDelivery.notification_id == row.notification_id)
      .order_by(NotificationDelivery.recipient_order)).all()
    if not deliveries:
        raise ApiError("INTERNAL_ERROR", "通知投递记录不完整")
    return {
        "notificationId": row.notification_id, "requestId": row.request_id, "sourceModule": row.source_module,
        "businessId": row.business_id, "spaceId": row.space_id, "kind": row.kind, "channel": row.channel,
        "title": row.title, "body": row.body, "createdAt": format_time(row.created_at),
        "deliveries": [{"recipientUserId": item.recipient_user_id, "status": item.status,
                        "attemptCount": item.attempt_count,
                        "lastAttemptAt": format_time(item.last_attempt_at) if item.last_attempt_at else None,
                        "reason": item.reason} for item in deliveries],
    }


def send_notification(*, input, actor_mode, source_module, user_id=None):
    try:
        request = SendNotificationInput.model_validate(input)
    except ValidationError:
        raise ApiError("INVALID_ARGUMENT", "通知输入不符合约定") from None
    _actor(actor_mode, source_module, user_id)
    if request.sourceModule.lower() != source_module:
        raise ApiError("FORBIDDEN", "通知来源与可信调用模块不一致")
    payload = request.model_dump(exclude={"requestId"})
    key = json.dumps(["campusflow:d:send_notification:v1", source_module, request.requestId],
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    lock_id = int.from_bytes(hashlib.sha256(key).digest()[:8], "big", signed=True)
    try:
        with session_factory("d").begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            # 缺少现存通知行时也能互斥；仅序列化同一来源和请求号，事务结束自动释放。
            db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})
            decision = check_permission_in_session(
                db, permission_code="d.notify.send", target_scope=_scope(request.spaceId),
                actor_mode=actor_mode, source_module=source_module, user_id=user_id,
            )
            if not decision["allowed"]:
                raise ApiError("FORBIDDEN", "没有目标范围的通知发送权限")
            previous = db.scalar(select(Notification).where(
                Notification.source_module == request.sourceModule, Notification.request_id == request.requestId,
            ))
            if previous is not None:
                if previous.input_data != payload:
                    raise ApiError("REQUEST_ID_REUSED", "通知请求编号已用于不同输入")
                result = _result(db, previous)
            else:
                recipients = {row.user_id: row for row in db.execute(select(
                    User.user_id, User.account_status, User.email,
                ).where(User.user_id.in_(request.recipientUserIds))).all()}
                if len(recipients) != len(request.recipientUserIds):
                    raise ApiError("NOT_FOUND", "通知收件人不存在")
                config = db.get(PublicConfig, 1)
                if config is None:
                    raise ApiError("UNAVAILABLE", "公共设置尚未初始化")
                now = utc_now()
                notice = Notification(
                    notification_id=f"ntf-{uuid4().hex}", source_module=request.sourceModule,
                    request_id=request.requestId, business_id=request.businessId, space_id=request.spaceId,
                    kind=request.kind, channel=config.notification_channel, title=request.title, body=request.body,
                    created_at=now, retry_limit=config.notification_retry_limit, input_data=payload,
                )
                db.add(notice)
                db.flush()
                for position, identifier in enumerate(request.recipientUserIds):
                    person = recipients[identifier]
                    address, reason = (None, "ACCOUNT_DISABLED") if person.account_status != "ACTIVE" else _mailbox(person.email)
                    pending = reason == "QUEUED"
                    db.add(NotificationDelivery(
                        notification_id=notice.notification_id, recipient_user_id=identifier, recipient_order=position,
                        recipient_email=address, status="PENDING" if pending else "FAILED", attempt_count=0,
                        last_attempt_at=None, next_attempt_at=now if pending else None,
                        attempt_token=None, lease_expires_at=None, reason=reason,
                    ))
                db.flush()
                result = _result(db, notice)
        return result
    except STORAGE_ERRORS:
        raise ApiError("UNAVAILABLE", "通知存储不可用或受理结果未确认，请保留原请求编号核实") from None


def get_notification(*, input, actor_mode, source_module, user_id=None):
    try:
        request = GetNotificationInput.model_validate(input)
    except ValidationError:
        raise ApiError("INVALID_ARGUMENT", "通知查询必须且只能提供一个有效编号") from None
    _actor(actor_mode, source_module, user_id, allow_direct=True)
    try:
        with session_factory("d")() as db:
            if actor_mode == "USER":
                decision = check_user_permission_in_session(
                    db, permission_code="d.notify.read", target_scope=None, user_id=user_id,
                )
            else:
                decision = check_permission_in_session(
                    db, permission_code="d.notify.read", target_scope=None,
                    actor_mode="SYSTEM", source_module=source_module,
                )
            if not decision["allowed"]:
                raise ApiError("FORBIDDEN", "没有通知查询权限")
            statement = select(Notification)
            if request.notificationId is not None:
                statement = statement.where(Notification.notification_id == request.notificationId)
            else:
                statement = statement.where(Notification.request_id == request.requestId)
            if actor_mode == "SYSTEM":
                statement = statement.where(Notification.source_module == source_module.upper())
            rows = db.scalars(statement).all()
            if not rows:
                raise ApiError("NOT_FOUND", "未找到可查询的通知，不能据此排除原请求仍在处理中")
            visible = []
            for row in rows:
                target = _scope(row.space_id)
                department = _target_department(db, target)
                if any(_covers(scope, target, department) for scope in decision["allowedScopes"]):
                    visible.append(row)
            if not visible:
                raise ApiError("FORBIDDEN", "通知不在当前授权范围内")
            if len(visible) > 1:
                raise ApiError("CONFLICT", "请求编号在多个可见来源中重复，请使用notificationId查询")
            return _result(db, visible[0])
    except STORAGE_ERRORS:
        raise ApiError("UNAVAILABLE", "通知存储暂时不可用，请保留原编号核实") from None
