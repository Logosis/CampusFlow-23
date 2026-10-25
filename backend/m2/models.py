"""M2 整教室预约与审批：本模块自己的 ORM 模型。

只描述本模块业务状态；教室、座位、用户与权限由 D 维护，整室占用由 M1 维护。
"""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """本模块 Base；迁移脚本与本模块 seed 共用。"""


def utc_now_storage() -> datetime:
    """存储用时间：统一 UTC、带时区。"""
    return datetime.now(timezone.utc)


class ApprovalConfig(Base):
    """整教室预约与审批配置（M2-S08）。初始为未配置状态。"""

    __tablename__ = "m2_approval_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    application_scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    approval_levels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    clock_start: Mapped[str | None] = mapped_column(String(32))
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    check_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now_storage
    )

    __table_args__ = (CheckConstraint("id = 1", name="ck_m2_config_single_row"),)


class Application(Base):
    """整教室预约申请（M2-S02）。"""

    __tablename__ = "m2_application"

    application_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    applicant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    space_id: Mapped[str] = mapped_column(String(64), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_level: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_level_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    termination_reason: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        Index("ix_m2_application_status", "status"),
        Index("ix_m2_application_applicant", "applicant_id"),
        Index("ix_m2_application_space", "space_id"),
    )


class Approval(Base):
    """逐级审批决定；历史记录不覆盖（M2-S04）。"""

    __tablename__ = "m2_approval"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("m2_application.application_id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    approver_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    application_version: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("application_id", "level", name="uq_m2_approval_level"),
        Index("ix_m2_approval_approver", "approver_id"),
    )


class SeatSync(Base):
    """M2 向 M1 发出的整室限制写回记录与核实结果（M1-S10/S11）。"""

    __tablename__ = "m2_seat_sync"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("m2_application.application_id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    handling: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result: Mapped[dict | None] = mapped_column(JSON)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now_storage
    )

    __table_args__ = (
        Index("ix_m2_seat_sync_application", "application_id"),
        Index("ix_m2_seat_sync_status", "status"),
    )


class NotificationRecord(Base):
    """本模块持久保留的通知受理情况；D 故障时用于补偿（D-S09）。"""

    __tablename__ = "m2_notification"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    notification: Mapped[dict | None] = mapped_column(JSON)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now_storage
    )

    __table_args__ = (
        UniqueConstraint("application_id", "kind", "request_id", name="uq_m2_notification"),
        Index("ix_m2_notification_status", "status"),
    )


class AuditRecord(Base):
    """未确认的审计追加，供补偿重试；业务记录本身不因审计失败删除。"""

    __tablename__ = "m2_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    audit_id: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now_storage
    )

    __table_args__ = (Index("ix_m2_audit_status", "status"),)


class RequestRecord(Base):
    """本模块请求编号去重：同编号同输入返回原结果，换内容报 REQUEST_ID_REUSED。"""

    __tablename__ = "m2_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    function: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(80), nullable=False)
    account_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now_storage
    )

    __table_args__ = (
        UniqueConstraint("function", "request_id", "account_key", name="uq_m2_request"),
    )
