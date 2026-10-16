from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.m1.models import OutboxEvent


def enqueue(db: Session, kind: str, request_id: str, payload: dict, now: datetime) -> None:
    exists = db.scalar(select(OutboxEvent.id).where(
        OutboxEvent.kind == kind,
        OutboxEvent.request_id == request_id,
    ).limit(1))
    if exists is not None:
        return
    db.add(OutboxEvent(
        kind=kind,
        request_id=request_id,
        payload=payload,
        status="PENDING",
        attempts=0,
        next_attempt_at=now,
        last_error="",
        created_at=now,
    ))


def notification(
    db: Session,
    request_id: str,
    business_id: str,
    space_id: str,
    recipient_user_id: str,
    kind: str,
    title: str,
    body: str,
    now: datetime,
) -> None:
    enqueue(db, "NOTIFICATION", request_id, {
        "requestId": request_id,
        "sourceModule": "M1",
        "businessId": business_id,
        "spaceId": space_id,
        "recipientUserIds": [recipient_user_id],
        "kind": kind,
        "title": title,
        "body": body,
    }, now)


def audit(
    db: Session,
    request_id: str,
    actor_type: str,
    actor_id: str | None,
    object_type: str,
    business_id: str,
    space_id: str | None,
    action: str,
    result: str,
    summary: str,
    now: datetime,
) -> None:
    enqueue(db, "AUDIT", request_id, {
        "operation": "APPEND",
        "requestId": request_id,
        "sourceModule": "M1",
        "actorType": actor_type,
        "actorId": actor_id,
        "objectType": object_type,
        "businessId": business_id,
        "spaceId": space_id,
        "action": action,
        "occurredAt": now.isoformat().replace("+00:00", "Z"),
        "result": result,
        "summary": summary,
    }, now)
