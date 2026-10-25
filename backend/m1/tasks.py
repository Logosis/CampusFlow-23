from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from backend.m1.models import OutboxEvent, Reservation, Violation
from backend.m1.outbox import audit, notification
from backend.shared.client import Actor, call_peer
from backend.shared.clock import utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError
from backend.shared.settings import Settings


def _queue_reservation_events() -> None:
    now = utc_now()
    factory = session_factory("m1")
    with factory() as db:
        rows = db.scalars(select(Reservation).where(
            Reservation.status.in_(("RESERVED", "CHECKED_IN"))
        ).with_for_update()).all()
        for row in rows:
            start = row.start_time if row.start_time.tzinfo else row.start_time.replace(tzinfo=now.tzinfo)
            end = row.end_time if row.end_time.tzinfo else row.end_time.replace(tzinfo=now.tzinfo)
            if row.status == "RESERVED":
                if now >= start - timedelta(minutes=15) and not row.before_start_notified:
                    row.before_start_notified = True
                    notification(
                        db, f"m1-before-{row.reservation_id}", row.reservation_id, row.space_id, row.user_id,
                        "M1_BEFORE_START", "预约即将开始", "预约将在15分钟内开始，请按时签到。", now,
                    )
                if now >= start + timedelta(minutes=10) and not row.not_checked_in_notified:
                    row.not_checked_in_notified = True
                    notification(
                        db, f"m1-unchecked-{row.reservation_id}", row.reservation_id, row.space_id, row.user_id,
                        "M1_NOT_CHECKED_IN", "预约尚未签到", "预约开始已超过10分钟，请尽快签到。", now,
                    )
                if now >= start + timedelta(minutes=15):
                    row.status = "CANCELLED_NO_SHOW"
                    row.version += 1
                    if not row.violation_recorded:
                        row.violation_recorded = True
                        db.add(Violation(
                            violation_id=f"vio-{uuid4().hex}", reservation_id=row.reservation_id,
                            user_id=row.user_id, occurred_at=now, reason="NO_SHOW",
                        ))
                    if not row.auto_cancel_notified:
                        row.auto_cancel_notified = True
                        notification(
                            db, f"m1-cancel-{row.reservation_id}", row.reservation_id, row.space_id, row.user_id,
                            "M1_AUTO_CANCELLED", "预约已自动取消",
                            "预约开始15分钟仍未签到，座位已释放并记录一次违约。", now,
                        )
                    audit(
                        db, f"m1-audit-cancel-{row.reservation_id}", "SYSTEM", "M1", "RESERVATION",
                        row.reservation_id, row.space_id, "AUTO_CANCEL_NO_SHOW", "SUCCESS", "未签到自动取消", now,
                    )
            elif row.status == "CHECKED_IN" and now >= end:
                row.status = "ENDED"
                row.version += 1
                audit(
                    db, f"m1-audit-ended-{row.reservation_id}", "SYSTEM", "M1", "RESERVATION",
                    row.reservation_id, row.space_id, "END", "SUCCESS", "预约到时结束", now,
                )
        db.commit()


async def _deliver_outbox() -> None:
    now = utc_now()
    factory = session_factory("m1")
    with factory() as db:
        ids = list(db.scalars(select(OutboxEvent.id).where(
            OutboxEvent.status.in_(("PENDING", "UNKNOWN")),
            OutboxEvent.next_attempt_at <= now,
        ).order_by(OutboxEvent.id).limit(20)).all())
    settings = Settings.load("m1")
    for event_id in ids:
        with factory() as db:
            event = db.get(OutboxEvent, event_id)
            if event is None or event.status not in {"PENDING", "UNKNOWN"}:
                continue
            event.attempts += 1
            try:
                if event.status == "PENDING":
                    function = "send_notification" if event.kind == "NOTIFICATION" else "audit"
                    await call_peer(settings, "d", function, event.payload, Actor("SYSTEM"))
                elif event.kind == "NOTIFICATION":
                    await call_peer(settings, "d", "get_notification", {"requestId": event.request_id}, Actor("SYSTEM"))
                else:
                    await call_peer(settings, "d", "audit", {
                        "operation": "GET", "requestId": event.request_id, "sourceModule": "M1"
                    }, Actor("SYSTEM"))
                event.status = "SENT"
                event.last_error = ""
            except ApiError as exc:
                event.last_error = exc.code
                if exc.code == "UNAVAILABLE" or (event.status == "UNKNOWN" and exc.code == "NOT_FOUND"):
                    event.status = "UNKNOWN"
                    event.next_attempt_at = now + timedelta(seconds=5)
                else:
                    event.status = "REJECTED"
            db.commit()


async def tick() -> None:
    _queue_reservation_events()
    await _deliver_outbox()
