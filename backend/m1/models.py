from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class M1Config(Base):
    __tablename__ = "m1_config"

    key: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    max_reservation_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class OpeningSchedule(Base):
    __tablename__ = "m1_opening_schedule"

    space_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ranges: Mapped[list["OpeningRange"]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan", order_by="OpeningRange.start_time"
    )


class OpeningRange(Base):
    __tablename__ = "m1_opening_range"
    __table_args__ = (Index("ix_m1_opening_space_time", "space_id", "start_time", "end_time"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[str] = mapped_column(
        ForeignKey("m1_opening_schedule.space_id", ondelete="CASCADE"), nullable=False
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule: Mapped[OpeningSchedule] = relationship(back_populates="ranges")


class Reservation(Base):
    __tablename__ = "m1_reservation"
    __table_args__ = (
        Index("ix_m1_reservation_seat_time", "seat_id", "start_time", "end_time"),
        Index("ix_m1_reservation_user_time", "user_id", "start_time"),
        Index("ix_m1_reservation_space_status", "space_id", "status"),
    )

    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    space_id: Mapped[str] = mapped_column(String(128), nullable=False)
    seat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="RESERVED")
    check_in_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    before_start_notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    not_checked_in_notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_cancel_notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    maintenance_cancel_notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    violation_recorded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CheckInCode(Base):
    __tablename__ = "m1_check_in_code"
    __table_args__ = (UniqueConstraint("space_id", "campus_date", name="uq_m1_code_space_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[str] = mapped_column(String(128), nullable=False)
    campus_date: Mapped[str] = mapped_column(String(10), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Violation(Base):
    __tablename__ = "m1_violation"
    __table_args__ = (UniqueConstraint("reservation_id", name="uq_m1_violation_reservation"),)

    violation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reservation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)


class IdempotencyRecord(Base):
    __tablename__ = "m1_idempotency"
    __table_args__ = (UniqueConstraint("function_name", "actor_key", "request_id", name="uq_m1_idempotency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    function_name: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LimitAggregate(Base):
    __tablename__ = "m1_room_limit"
    __table_args__ = (
        UniqueConstraint("source", "business_id", name="uq_m1_limit_business"),
        Index("ix_m1_limit_space_state", "space_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    business_id: Mapped[str] = mapped_column(String(128), nullable=False)
    space_id: Mapped[str] = mapped_column(String(128), nullable=False)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_processed_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LimitRequest(Base):
    __tablename__ = "m1_room_limit_request"
    __table_args__ = (
        UniqueConstraint("source", "request_id", name="uq_m1_limit_request"),
        Index("ix_m1_limit_request_business", "source", "business_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    business_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxEvent(Base):
    __tablename__ = "m1_outbox"
    __table_args__ = (UniqueConstraint("kind", "request_id", name="uq_m1_outbox_request"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ManagementLock(Base):
    __tablename__ = "m1_management_lock"

    resource_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
