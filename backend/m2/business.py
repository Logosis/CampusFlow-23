"""M2 业务实现：配置、申请、逐级审批、超时、撤销、查询、维修提示与补偿。

约定：
- 公开输入输出按 schemas.py；不擅自增加字段。
- 每次状态变更递增 version；审批记录只追加不覆盖。
- 通知、审计走 D；D 失败不删除已完成业务，未确认项持久保留并补偿。
- 对外调用使用原请求编号，结果未知时不换编号重发。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.shared.clock import CAMPUS_TZ, format_time, parse_time, utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError

from backend.m2 import peers, schemas
from backend.m2.models import (
    Application,
    Approval,
    ApprovalConfig,
    AuditRecord,
    NotificationRecord,
    RequestRecord,
    SeatSync,
)

M2 = "m2"
# 通知 kind 使用 D 的 M2 目录
KIND_PENDING = "M2_APPROVAL_PENDING"
KIND_APPROVED = "M2_APPROVED"
KIND_REJECTED = "M2_REJECTED"
KIND_TIMEOUT = "M2_TIMEOUT_CANCELLED"
KIND_CANCELLED = "M2_CANCELLED"

TERMINAL = {"APPROVED", "REJECTED", "TIMEOUT_CANCELLED", "CANCELLED"}
MAX_TEXT = 2000
DEFAULT_LIMIT = 20


# --------------------------------------------------------------------- 基础设施
@dataclass(frozen=True)
class Caller:
    """一次调用的身份与模式。"""

    user_id: str | None
    token: str | None
    mode: str

    @property
    def actor(self):
        if self.mode == "USER":
            return peers.user_actor(self.token or "")
        return peers.system_actor()

    @property
    def is_user(self) -> bool:
        return self.mode == "USER"


def session() -> Session:
    return session_factory("m2")()


def new_id() -> str:
    return uuid4().hex


def now() -> datetime:
    return utc_now()


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def t(value: datetime | None) -> str | None:
    return None if value is None else format_time(as_utc(value))


def require_text(value: str | None, field: str, *, allow_empty: bool = False) -> str:
    text = (value or "").strip()
    if not text and not allow_empty:
        raise ApiError("INVALID_ARGUMENT", f"{field}不能为空白")
    if len(text) > MAX_TEXT:
        raise ApiError("INVALID_ARGUMENT", f"{field}超过{MAX_TEXT}字符")
    return text


def _digest(payload: dict) -> str:
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             default=str).encode("utf-8")).hexdigest()


def encode_cursor(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str | None, signature: str = "") -> int:
    """游标只承载偏移与筛选签名；不合法或与当前筛选不符报 INVALID_ARGUMENT。"""
    if cursor is None:
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        offset = data["offset"]
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError
        if signature and data.get("s") != signature:
            raise ValueError
    except Exception as exc:  # noqa: BLE001
        raise ApiError("INVALID_ARGUMENT", "分页游标不合法或与当前筛选不匹配") from exc
    return offset


def limit_of(value: int | None) -> int:
    return DEFAULT_LIMIT if value is None else value


def paged(items: list, offset: int, limit: int, signature: str = "") -> dict:
    window = items[offset:offset + limit]
    nxt = offset + limit
    cursor = encode_cursor({"offset": nxt, "s": signature}) if nxt < len(items) else None
    return {"items": window, "nextCursor": cursor}


class RequestGuard:
    """请求编号去重：同编号同输入返回原结果；换内容报 REQUEST_ID_REUSED。"""

    def __init__(self, function: str, request_id: str, payload: dict, account_key: str = ""):
        self.function = function
        self.request_id = request_id
        self.payload = payload
        self.digest = _digest(payload)
        self.account_key = account_key

    def _select(self, db: Session) -> RequestRecord | None:
        return db.scalars(
            select(RequestRecord).where(
                RequestRecord.function == self.function,
                RequestRecord.request_id == self.request_id,
                RequestRecord.account_key == self.account_key,
            )
        ).first()

    def replay(self, db: Session) -> dict | None:
        record = self._select(db)
        if record is None:
            return None
        if record.input_digest != self.digest:
            raise ApiError("REQUEST_ID_REUSED", "同一请求编号不能用于不同业务输入")
        if record.result is None:
            raise ApiError("CONFLICT", "原请求结果尚未确认，请用原编号查询核实")
        return record.result

    def claim(self, db: Session) -> None:
        """在业务事务内登记本次请求；重复登记由唯一约束兜底。"""
        existing = self._select(db)
        if existing is not None:
            if existing.input_digest != self.digest:
                raise ApiError("REQUEST_ID_REUSED", "同一请求编号不能用于不同业务输入")
            return
        db.add(RequestRecord(function=self.function, request_id=self.request_id,
                             account_key=self.account_key, input_digest=self.digest))
        try:
            db.flush()
        except IntegrityError as exc:
            raise ApiError("CONFLICT", "原请求正在处理，请用原编号核实") from exc

    def store(self, db: Session, result: dict) -> None:
        record = self._select(db)
        if record is not None:
            record.result = result
            db.flush()


# --------------------------------------------------------------------- 配置
def get_config_row(db: Session, *, lock: bool = False) -> ApprovalConfig:
    row = db.scalar(select(ApprovalConfig).where(ApprovalConfig.id == 1).with_for_update()) if lock else db.get(ApprovalConfig, 1)
    if row is None:
        row = ApprovalConfig(
            id=1, version=1, application_scopes=[], approval_levels=[],
            clock_start=None, timeout_seconds=None, check_interval_seconds=None,
        )
        db.add(row)
        db.flush()
    return row


def application_config(application: Application):
    """在途规则只取提交快照；旧数据无法恢复原规则时拒绝猜测。"""
    from types import SimpleNamespace
    value = application.approval_config_snapshot
    if not isinstance(value, dict) or value.get("version") != application.config_version:
        raise ApiError("UNAVAILABLE", "原申请审批规则快照缺失，需要核对历史配置")
    return SimpleNamespace(approval_levels=value["approvalLevels"],
        application_scopes=value["applicationScopes"], clock_start=value["clockStart"],
        timeout_seconds=value["timeoutSeconds"], check_interval_seconds=value["checkIntervalSeconds"])


def config_view(row: ApprovalConfig) -> dict:
    return {
        "version": row.version,
        "applicationScopes": list(row.application_scopes or []),
        "approvalLevels": list(row.approval_levels or []),
        "clockStart": row.clock_start,
        "timeoutSeconds": row.timeout_seconds,
        "checkIntervalSeconds": row.check_interval_seconds,
    }


def config_ready(row: ApprovalConfig) -> bool:
    return bool(row.application_scopes) and bool(row.approval_levels)


# --------------------------------------------------------------------- 权限
async def permission_state(caller: Caller, code: str, scope: dict | None = None) -> dict:
    """D-S04；返回 allowed 与 allowedScopes，不把权限不足当成调用失败。"""
    return await peers.check_permission(code, scope, caller.actor)


async def require_permission(caller: Caller, code: str, scope: dict | None = None, *,
                             need_global: bool = False) -> dict:
    result = await permission_state(caller, code, scope)
    if not result.get("allowed"):
        raise ApiError("FORBIDDEN", f"缺少权限 {code} 或不在授权范围")
    if need_global and not _has_global(result.get("allowedScopes") or []):
        raise ApiError("FORBIDDEN", f"权限 {code} 需要全局范围")
    return result


def _has_global(scopes: Iterable[dict]) -> bool:
    return any(s.get("type") == "GLOBAL" for s in scopes)


def scope_allows(targets: list[dict], scope: dict | None) -> bool:
    """判断 scope 是否落在某个配置范围内。targets 为配置的 applicationScopes。"""
    if scope is None:
        return False
    kind = scope.get("type")
    sid = scope.get("id")
    for allowed in targets:
        akind, aid = allowed.get("type"), allowed.get("id")
        if akind == "GLOBAL":
            return True
        if kind == akind and sid == aid:
            return True
    return False


def scope_matches_config_or_space(row: ApprovalConfig, space: dict) -> bool:
    """申请须落在至少一个配置范围内：按配置范围与空间归属判断。"""
    if scope_allows(row.application_scopes or [], {"type": "GLOBAL", "id": None}):
        return True
    space_scope = {"type": "SPACE", "id": space["spaceId"]}
    if scope_allows(row.application_scopes or [], space_scope) and any(
        s.get("type") == "SPACE" for s in row.application_scopes or []
    ):
        return True
    department_id = space.get("departmentId")
    if department_id and scope_allows(
        row.application_scopes or [], {"type": "DEPARTMENT", "id": department_id}
    ):
        return True
    return False


# --------------------------------------------------------------------- 序列化
def application_view(db: Session, application: Application) -> dict:
    approvals = db.scalars(
        select(Approval).where(Approval.application_id == application.application_id)
        .order_by(Approval.level, Approval.decided_at)
    ).all()
    sync = db.scalars(
        select(SeatSync).where(SeatSync.application_id == application.application_id)
        .order_by(SeatSync.id.desc())
    ).first()
    notes = db.scalars(
        select(NotificationRecord)
        .where(NotificationRecord.application_id == application.application_id)
        .order_by(NotificationRecord.id)
    ).all()
    return {
        "application": {
            "applicationId": application.application_id,
            "applicantId": application.applicant_id,
            "spaceId": application.space_id,
            "startTime": t(application.start_time),
            "endTime": t(application.end_time),
            "purpose": application.purpose,
            "status": application.status,
            "currentLevel": application.current_level,
            "version": application.version,
            "configVersion": application.config_version,
            "submittedAt": t(application.submitted_at),
            "currentLevelStartedAt": t(application.current_level_started_at),
            "expiresAt": t(application.expires_at),
            "terminatedAt": t(application.terminated_at),
            "terminationReason": application.termination_reason or "",
        },
        "approvals": [
            {
                "approvalId": a.approval_id,
                "applicationId": a.application_id,
                "level": a.level,
                "approverId": a.approver_id,
                "decision": a.decision,
                "comment": a.comment,
                "decidedAt": t(a.decided_at),
                "applicationVersion": a.application_version,
            }
            for a in approvals
        ],
        "seatSync": None if sync is None else {
            "requestId": sync.request_id,
            "sourceVersion": sync.source_version,
            "status": sync.status,
            "result": sync.result,
            "checkedAt": t(sync.checked_at),
            "message": sync.message,
        },
        "notifications": [
            {
                "requestId": n.request_id,
                "status": n.status,
                "notification": n.notification,
                "message": n.message,
            }
            for n in notes
        ],
    }


def load_application(db: Session, application_id: str, *, lock: bool = False) -> Application:
    stmt = select(Application).where(Application.application_id == application_id)
    if lock:
        stmt = stmt.with_for_update()
    application = db.scalars(stmt).first()
    if application is None:
        raise ApiError("NOT_FOUND", "申请不存在")
    return application


def can_view(application: Application, caller: Caller) -> bool:
    return caller.is_user and application.applicant_id == caller.user_id


# --------------------------------------------------------------------- 时间与范围校验
def validate_period(start: str, end: str) -> tuple[datetime, datetime]:
    begin, finish = parse_time(start), parse_time(end)
    if begin >= finish:
        raise ApiError("INVALID_ARGUMENT", "开始时间必须早于结束时间")
    if begin <= now():
        raise ApiError("INVALID_ARGUMENT", "申请时段必须晚于当前时间")
    return begin, finish


def compute_expiry(row: ApprovalConfig, submitted_at: datetime,
                   level_started_at: datetime | None) -> datetime | None:
    if row.timeout_seconds is None or row.clock_start is None:
        return None
    base = submitted_at if row.clock_start == "SUBMISSION" else (level_started_at or submitted_at)
    return base + timedelta(seconds=int(row.timeout_seconds))


async def ensure_space_usable(space_id: str, begin: datetime, finish: datetime,
                              caller: Caller) -> dict:
    """D-S06 取空间并确认登记、可用；M1-S09 确认整室在目标时段可占用。"""
    space = await peers.get_space(space_id, caller.actor)
    if not isinstance(space, dict) or space.get("spaceId") != space_id:
        raise ApiError("UNAVAILABLE", "空间主数据返回格式不符合约定")
    if not space.get("registered") or not space.get("enabled"):
        raise ApiError("CONFLICT", "该空间未登记或不可用")
    availability = peers.availability_of(
        await peers.room_availability(space_id, format_time(begin), format_time(finish), caller.actor)
    )
    if not availability.canOccupy:
        detail = "、".join(availability.reasons) or "、".join(availability.blockedBy)
        raise ApiError("CONFLICT", f"该教室在所选时段不可占用：{detail}")
    return space


# --------------------------------------------------------------------- 通知与审计
async def queue_notification(db: Session, application: Application, kind: str,
                             recipient_ids: list[str], title: str, body: str) -> NotificationRecord:
    record = NotificationRecord(
        application_id=application.application_id, kind=kind, request_id=new_id(),
        recipient_ids=list(recipient_ids), status="PENDING",
    )
    db.add(record)
    db.flush()
    await deliver_notification(db, record, application)
    return record


async def deliver_notification(db: Session, record: NotificationRecord,
                               application: Application) -> None:
    record.attempts += 1
    payload = {
        "requestId": record.request_id,
        "sourceModule": "M2",
        "businessId": application.application_id,
        "spaceId": application.space_id,
        "recipientUserIds": list(record.recipient_ids),
        "kind": record.kind,
        "title": _notification_title(record.kind),
        "body": _notification_body(record.kind, application),
    }
    try:
        data = await peers.send_notification(payload)
    except ApiError as exc:
        record.status = "UNKNOWN" if exc.code in {"UNAVAILABLE", "INTERNAL_ERROR"} else "REJECTED"
        record.message = exc.message
        record.notification = None
    else:
        record.status = "ACCEPTED"
        record.notification = data
        record.message = ""
    db.flush()


def _notification_title(kind: str) -> str:
    return {
        KIND_PENDING: "整教室预约待审批",
        KIND_APPROVED: "整教室预约已通过",
        KIND_REJECTED: "整教室预约被驳回",
        KIND_TIMEOUT: "整教室预约超时撤销",
        KIND_CANCELLED: "整教室预约已撤销",
    }.get(kind, "整教室预约通知")


def _notification_body(kind: str, application: Application) -> str:
    return (
        f"{kind}: 教室 {application.space_id}，时段 {t(application.start_time)} 至 "
        f"{t(application.end_time)}，用途 {application.purpose}"
    )


async def queue_audit(db: Session, action: str, application: Application | None,
                      result: str, summary: str, *, request_id: str | None = None,
                      actor_id: str | None = None) -> None:
    payload = {
        "requestId": request_id or new_id(),
        "sourceModule": "M2",
        "actorType": "USER" if actor_id else "SYSTEM",
        "actorId": actor_id or "M2",
        "objectType": "M2_APPLICATION",
        "businessId": application.application_id if application is not None else "m2-approval-config",
        "spaceId": application.space_id if application is not None else None,
        "action": action,
        "occurredAt": format_time(now()),
        "result": result,
        "summary": summary,
    }
    record = AuditRecord(request_id=payload["requestId"], payload=payload, status="PENDING")
    db.add(record)
    db.flush()
    await flush_audit(record)


async def flush_audit(record: AuditRecord) -> None:
    record.attempts += 1
    try:
        data = await peers.audit_append(record.payload)
    except ApiError as exc:
        record.status = "UNKNOWN" if exc.code in {"UNAVAILABLE", "INTERNAL_ERROR"} else "REJECTED"
        record.message = exc.message
    else:
        record.status = "SUCCESS"
        record.audit_id = data.get("auditId") if isinstance(data, dict) else None
        record.message = ""


async def audit_detached(payload: dict) -> None:
    """与具体申请无关的事件（如配置变更）：独立事务记录，失败不影响已完成业务。"""
    from backend.shared.db import session_factory

    with session_factory("m2")() as db:
        record = AuditRecord(request_id=payload["requestId"], payload=payload, status="PENDING")
        db.add(record)
        db.flush()
        await flush_audit(record)
        db.commit()
