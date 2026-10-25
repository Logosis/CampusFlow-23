"""M2 公开服务的业务实现。

每个函数对应 M2.md 的一个服务编号；权限、业务关系、状态与版本都在这里校验。
外部依赖（D、M1）经 peers 调用真实接口；对方未就绪时按公共规则报错，不返回假成功。
"""
from __future__ import annotations

import logging
import time
from hashlib import sha256

from sqlalchemy import select, update

from backend.shared.clock import format_time
from backend.shared.errors import ApiError

from backend.m2 import business as core
from backend.m2 import peers
from backend.m2.models import Application, Approval, ApprovalConfig, AuditRecord, NotificationRecord, SeatSync

PENDING = "PENDING"
COMPENSATION_SECONDS = 5
logger = logging.getLogger(__name__)
_last_compensation = float("-inf")


# --------------------------------------------------------------------- 小工具
def _signature(*parts: object) -> str:
    return sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _level_approver(row: ApprovalConfig, level: int) -> str | None:
    for item in row.approval_levels or []:
        if item.get("level") == level:
            return item.get("approverId")
    return None


def _levels(row: ApprovalConfig) -> list[int]:
    return sorted(int(lv["level"]) for lv in (row.approval_levels or []))


def _require_pending(application: Application) -> None:
    if application.status != PENDING:
        raise ApiError("CONFLICT", "申请已终结，不能再审批或撤销")


def _check_version(application: Application, expected: int) -> None:
    if application.version != expected:
        raise ApiError("CONFLICT", "申请版本已变化，请重新查询后再提交")


def _page(rows: list, offset: int, limit: int, signature: str) -> dict:
    window = rows[offset:offset + limit]
    nxt = offset + limit
    return {
        "items": window,
        "nextCursor": (core.encode_cursor({"offset": nxt, "s": signature}) if nxt < len(rows) else None),
    }


def _business_state(status: str) -> str:
    return {
        "APPROVED": "APPROVED",
        "REJECTED": "REJECTED",
        "TIMEOUT_CANCELLED": "TIMEOUT_CANCELLED",
        "CANCELLED": "CANCELLED",
    }.get(status, "APPROVED")


# --------------------------------------------------------------------- S01
async def get_allowed_actions(payload: dict, caller: core.Caller) -> dict:
    space_scope = {"type": "SPACE", "id": payload["spaceId"]} if payload.get("spaceId") else None
    checks = [
        ("SUBMIT", "m2.application.submit"),
        ("READ", "m2.application.read"),
        ("CONFIGURE", "m2.config.read"),
        ("CANCEL", "m2.application.cancel"),
        ("MANAGED_READ", "m2.application.read_all"),
    ]
    actions = []
    for operation, code in checks:
        try:
            result = await core.permission_state(caller, code, space_scope)
            allowed = bool(result.get("allowed"))
            scopes = result.get("allowedScopes") or []
            reason = "" if allowed else f"缺少权限 {code} 或不在授权范围"
        except ApiError as exc:
            if exc.code in {"UNAVAILABLE", "INTERNAL_ERROR"}:
                raise
            allowed, scopes, reason = False, [], exc.message
        actions.append({"operation": operation, "allowed": allowed,
                        "allowedScopes": scopes, "reason": reason})
    with core.session() as db:
        row = core.get_config_row(db)
        approve = any(lv.get("approverId") == caller.user_id for lv in (row.approval_levels or []))
        pending = select(Application).where(Application.status == PENDING)
        if payload.get("spaceId"):
            pending = pending.where(Application.space_id == payload["spaceId"])
        for application in db.scalars(pending):
            snapshot = application.approval_config_snapshot or {}
            approve = approve or any(level.get("level") == application.current_level
                and level.get("approverId") == caller.user_id for level in snapshot.get("approvalLevels", []))
        db.commit()
    decision = await core.permission_state(caller, "m2.approval.approve", space_scope)
    approve = approve and bool(decision.get("allowed"))
    actions.append({
        "operation": "APPROVE",
        "allowed": approve,
        "allowedScopes": decision.get("allowedScopes", []) if approve else [],
        "reason": "" if approve else "没有审批权限或当前负责的审批任务",
    })
    order = ["SUBMIT", "APPROVE", "READ", "CONFIGURE", "CANCEL", "MANAGED_READ"]
    actions.sort(key=lambda item: order.index(item["operation"]))
    return {"actions": actions}


# --------------------------------------------------------------------- S10
async def get_classroom_options(payload: dict, caller: core.Caller) -> dict:
    await core.require_permission(caller, "m2.application.submit")
    start, end = payload.get("startTime"), payload.get("endTime")
    if (start is None) != (end is None):
        raise ApiError("INVALID_ARGUMENT", "开始与结束时间必须同时提供或同时省略")
    data = await peers.list_spaces({"departmentId": payload.get("departmentId")}, caller.actor)
    spaces = data.get("items") if isinstance(data, dict) else None
    if not isinstance(spaces, list):
        raise ApiError("UNAVAILABLE", "空间列表返回格式不符合约定")
    signature = _signature("spaces", payload.get("departmentId"))
    offset = core.decode_cursor(payload.get("cursor"), signature)
    limit = core.limit_of(payload.get("limit"))
    page = spaces[offset:offset + limit]
    items = []
    for space in page:
        availability = None
        if start is not None:
            availability = peers.availability_of(
                await peers.room_availability(space["spaceId"], start, end, caller.actor)
            ).model_dump()
        items.append({"space": space, "availability": availability})
    nxt = offset + limit
    return {
        "items": items,
        "nextCursor": (core.encode_cursor({"offset": nxt, "s": signature}) if nxt < len(spaces) else None),
    }


# --------------------------------------------------------------------- S02
async def submit_application(payload: dict, caller: core.Caller) -> dict:
    await core.require_permission(caller, "m2.application.submit",
                                  {"type": "SPACE", "id": payload["spaceId"]})
    guard = core.RequestGuard("submit_application", payload["requestId"], payload, caller.user_id or "")
    with core.session() as db:
        replayed = guard.replay(db)
        if replayed is not None:
            return replayed
        row = core.get_config_row(db)
        if not core.config_ready(row):
            raise ApiError("CONFLICT", "整教室预约尚未配置，不能提交申请")
        db.commit()

    begin, finish = core.validate_period(payload["startTime"], payload["endTime"])
    purpose = core.require_text(payload["purpose"], "用途")
    space = await core.ensure_space_usable(payload["spaceId"], begin, finish, caller)

    with core.session() as db:
        row = core.get_config_row(db)
        if not core.scope_matches_config_or_space(row, space):
            raise ApiError("FORBIDDEN", "该教室不在当前允许申请的范围内")
        guard.claim(db)
        submitted = core.now()
        first = _level_approver(row, 1)
        if first is None:
            raise ApiError("CONFLICT", "审批配置不完整，不能提交申请")
        application = Application(
            application_id=core.new_id(),
            applicant_id=caller.user_id or "",
            space_id=payload["spaceId"],
            start_time=begin,
            end_time=finish,
            purpose=purpose,
            status=PENDING,
            current_level=1,
            version=1,
            config_version=row.version,
            approval_config_snapshot=core.config_view(row),
            submitted_at=submitted,
            current_level_started_at=submitted,
            expires_at=core.compute_expiry(row, submitted, submitted),
        )
        db.add(application)
        db.flush()
        await _notify(db, application, core.KIND_PENDING, [first])
        await core.queue_audit(db, "SUBMIT_APPLICATION", application, "SUCCESS", "提交整教室预约申请",
                               actor_id=caller.user_id)
        result = core.application_view(db, application)
        guard.store(db, result)
        db.commit()
    return await _dispatch_application(application.application_id, result, guard)


async def _notify(db, application: Application, kind: str, recipients: list[str]) -> None:
    await core.queue_notification(
        db, application, kind, recipients,
        core._notification_title(kind), core._notification_body(kind, application))


# --------------------------------------------------------------------- S03
async def get_approval_tasks(payload: dict, caller: core.Caller) -> dict:
    await core.require_permission(caller, "m2.approval.approve")
    with core.session() as db:
        filters = [Application.status == PENDING]
        if payload.get("applicationId"):
            filters.append(Application.application_id == payload["applicationId"])
        if payload.get("spaceId"):
            filters.append(Application.space_id == payload["spaceId"])
        rows = db.scalars(select(Application).where(*filters)
                          .order_by(Application.submitted_at.desc(), Application.application_id)).all()
        rows = [item for item in rows if _level_approver(core.application_config(item),
                item.current_level or 0) == caller.user_id]
        signature = _signature("tasks", caller.user_id, payload.get("applicationId"),
                               payload.get("spaceId"))
        offset = core.decode_cursor(payload.get("cursor"), signature)
        limit = core.limit_of(payload.get("limit"))
        views = [core.application_view(db, item) for item in rows]
        db.rollback()
    return _page(views, offset, limit, signature)


# --------------------------------------------------------------------- S04
async def submit_approval(payload: dict, caller: core.Caller) -> dict:
    with core.session() as db:
        space_id = core.load_application(db, payload["applicationId"]).space_id
    await core.require_permission(caller, "m2.approval.approve", {"type": "SPACE", "id": space_id})
    guard = core.RequestGuard("submit_approval", payload["requestId"], payload, caller.user_id or "")
    with core.session() as db:
        replayed = guard.replay(db)
        if replayed is not None:
            return replayed
        pending = core.load_application(db, payload["applicationId"])
        _require_pending(pending)
        _check_version(pending, payload["expectedVersion"])
        approver = _level_approver(core.application_config(pending), payload["level"])
        if caller.user_id != approver:
            raise ApiError("FORBIDDEN", "不是本申请当前级别的审批人")
    await _verify_approver(approver, pending, caller)

    with core.session() as db:
        application = core.load_application(db, payload["applicationId"], lock=True)
        _require_pending(application)
        _check_version(application, payload["expectedVersion"])
        row = core.application_config(application)
        level = payload["level"]
        if application.current_level != level:
            raise ApiError("CONFLICT", "不是当前审批级别，不能越级审批")
        expected_approver = _level_approver(row, level)
        if expected_approver is None:
            raise ApiError("CONFLICT", "审批配置缺少当前级别")
        if caller.user_id != expected_approver:
            raise ApiError("FORBIDDEN", "不是本申请当前级别的审批人")
        guard.claim(db)
        comment = core.require_text(payload.get("comment"), "审批意见", allow_empty=True)

        levels = _levels(row)
        last_level = level == (max(levels) if levels else level)
        reject = payload["decision"] == "REJECT"
        decided = core.now()
        new_version = application.version + 1
        db.add(Approval(
            approval_id=core.new_id(), application_id=application.application_id, level=level,
            approver_id=caller.user_id or "", decision=payload["decision"], comment=comment,
            decided_at=decided, application_version=new_version,
        ))
        if reject:
            application.status = "REJECTED"
            application.current_level = None
            application.terminated_at = decided
            application.termination_reason = "审批驳回"
            action, state = "RELEASE", "REJECTED"
        elif last_level:
            application.status = "APPROVED"
            application.current_level = None
            action, state = "OCCUPY", "APPROVED"
        else:
            application.current_level = level + 1
            application.current_level_started_at = decided
            application.expires_at = core.compute_expiry(row, application.submitted_at, decided)
            action, state = None, None
        application.version = new_version
        db.flush()
        if action is not None:
            sync = SeatSync(application_id=application.application_id, action=action,
                            request_id=core.new_id(), source_version=new_version,
                            status="PENDING", handling=True, message="", attempts=0)
            db.add(sync)
            db.flush()
        if reject:
            await _notify(db, application, core.KIND_REJECTED, [application.applicant_id])
        elif last_level:
            await _notify(db, application, core.KIND_APPROVED, [application.applicant_id])
        else:
            nxt = _level_approver(row, level + 1)
            if nxt:
                await _notify(db, application, core.KIND_PENDING, [nxt])
        await core.queue_audit(db, "SUBMIT_APPROVAL", application, "SUCCESS",
                               f"第{level}级审批 {payload['decision']}", actor_id=caller.user_id)
        application_id = application.application_id
        result = core.application_view(db, application)
        guard.store(db, result)
        db.commit()
    return await _dispatch_application(application_id, result, guard)


async def _verify_approver(approver_id: str, application: Application, caller: core.Caller) -> None:
    """每次审批前重查审批人是否仍为现任、启用且具备审批资格。"""
    people = await peers.people_by_ids([approver_id],
                                       {"type": "SPACE", "id": application.space_id}, caller.actor)
    if not people or people[0].get("userId") != approver_id:
        raise ApiError("CONFLICT", "审批人已不存在")
    person = people[0]
    if person.get("accountStatus") != "ACTIVE" or person.get("eligible") is not True:
        raise ApiError("CONFLICT", "当前审批人已停用或不再具备审批资格")


async def _apply_sync(db, application: Application, sync: SeatSync) -> None:
    """调用 M1-S10 写回整室限制；确认成功才算成功，未知不冒充成功。"""
    payload = {
        "source": "M2",
        "businessId": application.application_id,
        "spaceId": application.space_id,
        "action": sync.action,
        "businessState": _business_state(application.status),
        "startTime": format_time(core.as_utc(application.start_time)),
        "endTime": format_time(core.as_utc(application.end_time)),
        "sourceVersion": sync.source_version,
        "requestId": sync.request_id,
    }
    sync.attempts += 1
    try:
        data = await peers.change_room_limit(payload)
    except ApiError as exc:
        if exc.code in {"FORBIDDEN", "NOT_FOUND", "CONFLICT", "INVALID_ARGUMENT"}:
            sync.status = "REJECTED"
            sync.checked_at = core.now()
        else:
            sync.status = "UNKNOWN"
        sync.message = exc.message
        sync.result = None
    else:
        sync.result = data
        sync.message = data.get("message") or ""
        sync.checked_at = core.now()
        request_status = data.get("requestStatus")
        if request_status == "SUCCESS":
            sync.status = "SUCCESS"
        elif request_status == "REJECTED":
            # M1 明确拒绝（如占用冲突）：如实记录，不改写为成功
            sync.status = "REJECTED"
        else:
            sync.status = "UNKNOWN"
    sync.handling = sync.status in {"PENDING", "UNKNOWN"}
    db.flush()


async def _dispatch_application(application_id: str, original: dict | None = None,
                                guard: core.RequestGuard | None = None) -> dict | None:
    """业务及待办已提交后再访问外部服务，不在持有申请行锁时等待网络。"""
    with core.session() as db:
        syncs = list(db.scalars(select(SeatSync.id).where(SeatSync.application_id == application_id,
            SeatSync.status.in_(["PENDING", "UNKNOWN"]))))
        notes = list(db.scalars(select(NotificationRecord.id).where(NotificationRecord.application_id == application_id,
            NotificationRecord.status.in_(["PENDING", "UNKNOWN"]))))
        audits = list(db.scalars(select(AuditRecord.id).where(AuditRecord.payload["businessId"].as_string() == application_id,
            AuditRecord.status.in_(["PENDING", "UNKNOWN"]))))
    for ident in syncs:
        await _retry_sync(ident)
    for ident in notes:
        await _retry_notification(ident)
    for ident in audits:
        await _retry_audit(ident)
    if original is None:
        return None
    with core.session() as db:
        current = core.application_view(db, core.load_application(db, application_id))
        # 原操作的业务版本和审批记录不被后续操作替换，仅更新原请求的投递回执。
        if original["seatSync"] is not None and current["seatSync"] is not None:
            if original["seatSync"]["requestId"] == current["seatSync"]["requestId"]:
                original["seatSync"] = current["seatSync"]
        notes = {n["requestId"]: n for n in current["notifications"]}
        original["notifications"] = [notes.get(n["requestId"], n) for n in original["notifications"]]
        if guard is not None:
            guard.store(db, original)
        db.commit()
    return original


# --------------------------------------------------------------------- S05 / S06
async def get_application(payload: dict, caller: core.Caller) -> dict:
    with core.session() as db:
        application = core.load_application(db, payload["applicationId"])
        await _ensure_visible(application, caller)
        result = core.application_view(db, application)
        db.rollback()
    return result


async def _ensure_visible(application: Application, caller: core.Caller) -> None:
    if caller.is_user and application.applicant_id == caller.user_id:
        return
    with core.session() as db:
        handled = db.scalars(select(Approval).where(
            Approval.application_id == application.application_id,
            Approval.approver_id == caller.user_id)).first()
        db.rollback()
    if handled is not None:
        return
    await core.require_permission(caller, "m2.application.read_all",
                                  {"type": "SPACE", "id": application.space_id})


async def get_applications(payload: dict, caller: core.Caller) -> dict:
    start, end = payload.get("startTime"), payload.get("endTime")
    if (start is None) != (end is None):
        raise ApiError("INVALID_ARGUMENT", "开始与结束时间必须同时提供或同时省略")
    view = payload["view"]
    with core.session() as db:
        filters = []
        if view == "OWN":
            await core.require_permission(caller, "m2.application.read")
            filters.append(Application.applicant_id == caller.user_id)
        elif view == "HANDLED":
            await core.require_permission(caller, "m2.application.read")
            handled_ids = select(Approval.application_id).where(
                Approval.approver_id == caller.user_id)
            filters.append(Application.application_id.in_(handled_ids))
        else:
            await core.require_permission(caller, "m2.application.read_all")
        if payload.get("status"):
            filters.append(Application.status == payload["status"])
        if payload.get("spaceId"):
            filters.append(Application.space_id == payload["spaceId"])
        if payload.get("applicationId"):
            filters.append(Application.application_id == payload["applicationId"])
        if start is not None:
            filters.append(Application.submitted_at >= core.parse_time(start))
            filters.append(Application.submitted_at < core.parse_time(end))
        rows = db.scalars(select(Application).where(*filters)
                          .order_by(Application.submitted_at.desc(),
                                    Application.application_id)).all()
        signature = _signature("applications", view, caller.user_id, payload.get("status"),
                               start, end, payload.get("spaceId"),
                               payload.get("applicationId"), payload.get("limit"))
        offset = core.decode_cursor(payload.get("cursor"), signature)
        limit = core.limit_of(payload.get("limit"))
        views = [core.application_view(db, item) for item in rows]
        db.rollback()
    return _page(views, offset, limit, signature)


# --------------------------------------------------------------------- S07
async def cancel_application(payload: dict, caller: core.Caller) -> dict:
    with core.session() as db:
        space_id = core.load_application(db, payload["applicationId"]).space_id
    await core.require_permission(caller, "m2.application.cancel", {"type": "SPACE", "id": space_id})
    guard = core.RequestGuard("cancel_application", payload["requestId"], payload, caller.user_id or "")
    with core.session() as db:
        replayed = guard.replay(db)
        if replayed is not None:
            return replayed
    reason = core.require_text(payload.get("reason"), "撤销原因", allow_empty=True)
    with core.session() as db:
        application = core.load_application(db, payload["applicationId"], lock=True)
        if not caller.is_user or application.applicant_id != caller.user_id:
            raise ApiError("FORBIDDEN", "只能撤销本人的申请")
        _require_pending(application)
        _check_version(application, payload["expectedVersion"])
        guard.claim(db)
        application.status = "CANCELLED"
        application.current_level = None
        application.version += 1
        application.terminated_at = core.now()
        application.termination_reason = reason or "申请人主动撤销"
        db.flush()
        sync = SeatSync(application_id=application.application_id, action="RELEASE",
                        request_id=core.new_id(), source_version=application.version,
                        status="PENDING", handling=True, message="", attempts=0)
        db.add(sync)
        db.flush()
        await _notify(db, application, core.KIND_CANCELLED, [application.applicant_id])
        await core.queue_audit(db, "CANCEL_APPLICATION", application, "SUCCESS", "申请人主动撤销",
                               actor_id=caller.user_id)
        result = core.application_view(db, application)
        guard.store(db, result)
        db.commit()
    return await _dispatch_application(application.application_id, result, guard)


# --------------------------------------------------------------------- S08
async def get_approval_config(payload: dict, caller: core.Caller) -> dict:
    await core.require_permission(caller, "m2.config.read", need_global=True)
    with core.session() as db:
        row = core.get_config_row(db)
        result = core.config_view(row)
        db.commit()
    return result


async def update_approval_config(payload: dict, caller: core.Caller) -> dict:
    await core.require_permission(caller, "m2.config.manage", need_global=True)
    guard = core.RequestGuard("update_approval_config", payload["requestId"], payload)
    with core.session() as db:
        replayed = guard.replay(db)
        if replayed is not None:
            return replayed
    scopes = _validate_scopes(payload["applicationScopes"])
    levels = _validate_levels(payload["approvalLevels"])
    await _validate_approvers(levels, scopes, caller)
    with core.session() as db:
        row = core.get_config_row(db, lock=True)
        if row.version != payload["expectedVersion"]:
            raise ApiError("CONFLICT", "配置版本已变化，请重新查询后再提交")
        guard.claim(db)
        row.application_scopes = scopes
        row.approval_levels = levels
        row.clock_start = payload["clockStart"]
        row.timeout_seconds = payload["timeoutSeconds"]
        row.check_interval_seconds = payload["checkIntervalSeconds"]
        row.version += 1
        result = core.config_view(row)
        guard.store(db, result)
        db.commit()
    await core.audit_detached({
        "requestId": core.new_id(),
        "sourceModule": "M2",
        "actorType": "USER" if caller.is_user else "SYSTEM",
        "actorId": caller.user_id if caller.is_user else "M2",
        "objectType": "M2_APPROVAL_CONFIG",
        "businessId": "m2-approval-config",
        "spaceId": None,
        "action": "UPDATE_APPROVAL_CONFIG",
        "occurredAt": format_time(core.now()),
        "result": "SUCCESS",
        "summary": "修改整教室预约配置",
    })
    return result


def _validate_scopes(scopes: list) -> list[dict]:
    if not scopes:
        raise ApiError("INVALID_ARGUMENT", "申请范围不能为空")
    seen = set()
    result = []
    for scope in scopes:
        kind = scope.get("type")
        sid = scope.get("id")
        if kind not in {"GLOBAL", "DEPARTMENT", "SPACE"}:
            raise ApiError("INVALID_ARGUMENT", "申请范围类型不合法")
        if kind == "GLOBAL":
            if sid is not None:
                raise ApiError("INVALID_ARGUMENT", "GLOBAL 范围的 id 必须为 null")
        elif not sid:
            raise ApiError("INVALID_ARGUMENT", f"{kind} 范围必须给出 id")
        key = (kind, sid)
        if key in seen:
            raise ApiError("INVALID_ARGUMENT", "申请范围不能重复")
        seen.add(key)
        result.append({"type": kind, "id": sid})
    return result


def _validate_levels(levels: list) -> list[dict]:
    if len(levels) < 2:
        raise ApiError("INVALID_ARGUMENT", "审批至少两级")
    ordered = sorted(levels, key=lambda item: int(item.get("level", 0)))
    expected = list(range(1, len(ordered) + 1))
    if [int(item.get("level", 0)) for item in ordered] != expected:
        raise ApiError("INVALID_ARGUMENT", "审批级别必须从 1 开始连续排列")
    approvers = [item.get("approverId") for item in ordered]
    if len(set(approvers)) != len(approvers):
        raise ApiError("INVALID_ARGUMENT", "每级必须各有一名审批人，不能重复")
    return [{"level": int(item["level"]), "approverId": item["approverId"]} for item in ordered]


async def _validate_approvers(levels: list[dict], scopes: list[dict], caller: core.Caller) -> None:
    target = {"type": scopes[0]["type"], "id": scopes[0]["id"]}
    ids = [lv["approverId"] for lv in levels]
    people = await peers.people_by_ids(ids, target, caller.actor)
    by_id = {person.get("userId"): person for person in people}
    for user_id in ids:
        person = by_id.get(user_id)
        if person is None:
            raise ApiError("NOT_FOUND", f"审批人 {user_id} 不存在")
        if person.get("accountStatus") != "ACTIVE":
            raise ApiError("INVALID_ARGUMENT", f"审批人 {user_id} 已停用")
        if person.get("eligible") is not True:
            raise ApiError("INVALID_ARGUMENT", f"审批人 {user_id} 不具备整教室审批权限")


# --------------------------------------------------------------------- S09
async def get_maintenance_impact(payload: dict, caller: core.Caller) -> dict:
    with core.session() as db:
        application = core.load_application(db, payload["applicationId"])
        await _ensure_visible(application, caller)
        space_id = application.space_id
        start = format_time(core.as_utc(application.start_time))
        end = format_time(core.as_utc(application.end_time))
        db.rollback()
    availability = peers.availability_of(await peers.room_availability(space_id, start, end, caller.actor))
    return {
        "applicationId": payload["applicationId"],
        "affectedByMaintenance": "M3" in availability.blockedBy,
        "availability": availability.model_dump(),
    }


# --------------------------------------------------------------------- 后台任务
async def tick() -> None:
    """两阶段均尝试；未完成轮次交由共同worker保留健康错误并在下一轮重试。"""
    failed = False
    for phase, run in (("timeout_scan", _timeout_scan), ("compensate", _compensate)):
        try:
            await run()
        except Exception as exc:
            failed = True
            logger.error("M2 tick phase=%s failed type=%s", phase, type(exc).__name__)
    if failed:
        raise ApiError("UNAVAILABLE", "M2后台轮次未完成，请检查超时扫描与补偿")


async def _timeout_scan() -> None:
    with core.session() as db:
        row = core.get_config_row(db)
        interval = row.check_interval_seconds
        db.commit()
    if interval is None or interval <= 0:
        return
    with core.session() as db:
        due = db.scalars(select(Application).where(
            Application.status == PENDING, Application.expires_at.is_not(None),
            Application.expires_at <= core.now())).all()
        db.commit()
    for item in due:
        await _expire(item.application_id)


async def _expire(application_id: str) -> None:
    with core.session() as db:
        application = core.load_application(db, application_id, lock=True)
        if application.status != PENDING or application.expires_at is None:
            db.rollback()
            return
        if core.as_utc(application.expires_at) > core.now():
            db.rollback()
            return
        new_version = application.version + 1
        updated = db.execute(
            update(Application)
            .where(Application.application_id == application_id,
                   Application.status == PENDING,
                   Application.version == application.version)
            .values(status="TIMEOUT_CANCELLED", current_level=None,
                    version=new_version, terminated_at=core.now(),
                    termination_reason="待审批超时自动撤销")
        ).rowcount
        if updated != 1:
            db.rollback()
            return
        db.flush()
        application = core.load_application(db, application_id)
        sync = SeatSync(application_id=application_id, action="RELEASE",
                        request_id=core.new_id(), source_version=new_version,
                        status="PENDING", handling=True, message="", attempts=0)
        db.add(sync)
        db.flush()
        await _notify(db, application, core.KIND_TIMEOUT, [application.applicant_id])
        await core.queue_audit(db, "TIMEOUT_CANCEL", application, "SUCCESS", "待审批超时自动撤销")
        db.commit()
    await _dispatch_application(application_id)


async def _compensate() -> None:
    """补偿未确认的写回、通知与审计；不换编号，未确认就不冒充成功。"""
    global _last_compensation
    now = time.monotonic()
    if now - _last_compensation < COMPENSATION_SECONDS:
        return
    _last_compensation = now
    with core.session() as db:
        syncs = db.scalars(select(SeatSync).where(SeatSync.status == "UNKNOWN")
                           .limit(10)).all()
        notes = db.scalars(select(NotificationRecord).where(
            NotificationRecord.status.in_(["PENDING", "UNKNOWN"])).limit(10)).all()
        audits = db.scalars(select(AuditRecord).where(
            AuditRecord.status.in_(["PENDING", "UNKNOWN"])).limit(10)).all()
        sync_ids = [s.id for s in syncs]
        note_ids = [n.id for n in notes]
        audit_ids = [a.id for a in audits]
        db.rollback()
    for sync_id in sync_ids:
        await _retry_sync(sync_id)
    for note_id in note_ids:
        await _retry_notification(note_id)
    for audit_id in audit_ids:
        await _retry_audit(audit_id)


async def _retry_sync(sync_id: int) -> None:
    with core.session() as db:
        sync = db.get(SeatSync, sync_id)
        if sync is None or sync.status not in {"PENDING", "UNKNOWN"}:
            db.rollback()
            return
        application = core.load_application(db, sync.application_id)
        if sync.status == "UNKNOWN" and sync.attempts >= 1:
            verified = await _verify_sync_with_m1(application, sync)
            if verified:
                db.commit()
                return
        await _apply_sync(db, application, sync)
        db.commit()


async def _verify_sync_with_m1(application: Application, sync: SeatSync) -> bool:
    """用 M1-S11 核实原写回编号；核实失败不推断原请求被拒绝。"""
    try:
        data = await peers.room_limit("M2", application.application_id, sync.request_id)
    except ApiError:
        return False
    sync.result = data
    sync.message = data.get("message") or sync.message
    status = data.get("requestStatus")
    if status == "SUCCESS":
        sync.status = "SUCCESS"
        sync.checked_at = core.now()
    elif status == "REJECTED":
        sync.status = "REJECTED"
        sync.checked_at = core.now()
    else:
        return False
    sync.handling = False
    return True


async def _retry_notification(note_id: int) -> None:
    with core.session() as db:
        record = db.get(NotificationRecord, note_id)
        if record is None or record.status not in {"PENDING", "UNKNOWN"}:
            db.rollback()
            return
        application = core.load_application(db, record.application_id)
        await core.deliver_notification(db, record, application)
        db.commit()


async def _retry_audit(audit_id: int) -> None:
    with core.session() as db:
        record = db.get(AuditRecord, audit_id)
        if record is None or record.status not in {"PENDING", "UNKNOWN"}:
            db.rollback()
            return
        await core.flush_audit(record)
        db.commit()
