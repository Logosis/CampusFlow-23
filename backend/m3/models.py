"""M3-owned durable business state; external IDs never reference another database."""
from datetime import datetime
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorkOrder(Base):
    __tablename__ = "m3_work_orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reporter_id: Mapped[str] = mapped_column(Text)
    space_id: Mapped[str] = mapped_column(Text, index=True)
    department_id: Mapped[str | None] = mapped_column(Text)
    fault_location: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    assignee_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stage_entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_repair_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timeout_config_version: Mapped[int | None] = mapped_column(Integer)


class Record(Base):
    __tablename__ = "m3_records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("m3_work_orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON)


class StageCycle(Base):
    __tablename__ = "m3_stage_cycles"
    __table_args__ = (UniqueConstraint("work_order_id", "stage", "cycle"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("m3_work_orders.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    cycle: Mapped[int] = mapped_column(Integer)
    config_version: Mapped[int] = mapped_column(Integer)
    rule: Mapped[dict | None] = mapped_column(JSON)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    escalation_id: Mapped[str | None] = mapped_column(String(64))
    exited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Outbox(Base):
    __tablename__ = "m3_outbox"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_order_id: Mapped[str | None] = mapped_column(ForeignKey("m3_work_orders.id"), index=True)
    record_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    request_id: Mapped[str] = mapped_column(String(128), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    selector: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict | None] = mapped_column(JSON)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class Receipt(Base):
    __tablename__ = "m3_receipts"
    __table_args__ = (UniqueConstraint("owner", "function", "request_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(Text)
    function: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    work_order_id: Mapped[str | None] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)


class TimeoutConfig(Base):
    __tablename__ = "m3_timeout_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class WorkerState(Base):
    __tablename__ = "m3_worker_state"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
