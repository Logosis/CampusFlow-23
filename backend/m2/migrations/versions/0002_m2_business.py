"""M2 business tables: applications, approvals, room-limit sync, notifications, audit, requests.

Revision ID: m2_0002
Revises: m2_0001
"""
import sqlalchemy as sa
from alembic import op

revision = "m2_0002"
down_revision = "m2_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "m2_approval_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("application_scopes", sa.JSON(), nullable=False),
        sa.Column("approval_levels", sa.JSON(), nullable=False),
        sa.Column("clock_start", sa.String(length=32), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("check_interval_seconds", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_m2_config_single_row"),
    )

    op.create_table(
        "m2_application",
        sa.Column("application_id", sa.String(length=64), primary_key=True),
        sa.Column("applicant_id", sa.String(length=64), nullable=False),
        sa.Column("space_id", sa.String(length=64), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_level", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_level_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("termination_reason", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_m2_application_status", "m2_application", ["status"])
    op.create_index("ix_m2_application_applicant", "m2_application", ["applicant_id"])
    op.create_index("ix_m2_application_space", "m2_application", ["space_id"])

    op.create_table(
        "m2_approval",
        sa.Column("approval_id", sa.String(length=64), primary_key=True),
        sa.Column("application_id", sa.String(length=64),
                  sa.ForeignKey("m2_application.application_id", ondelete="CASCADE"), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("approver_id", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("application_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("application_id", "level", name="uq_m2_approval_level"),
    )
    op.create_index("ix_m2_approval_approver", "m2_approval", ["approver_id"])

    op.create_table(
        "m2_seat_sync",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("application_id", sa.String(length=64),
                  sa.ForeignKey("m2_application.application_id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("handling", sa.Boolean(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_m2_seat_sync_application", "m2_seat_sync", ["application_id"])
    op.create_index("ix_m2_seat_sync_status", "m2_seat_sync", ["status"])

    op.create_table(
        "m2_notification",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("notification", sa.JSON(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("application_id", "kind", "request_id", name="uq_m2_notification"),
    )
    op.create_index("ix_m2_notification_status", "m2_notification", ["status"])

    op.create_table(
        "m2_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("audit_id", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_m2_audit_status", "m2_audit", ["status"])

    op.create_table(
        "m2_request",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("function", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("account_key", sa.String(length=64), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("function", "request_id", "account_key", name="uq_m2_request"),
    )


def downgrade() -> None:
    op.drop_table("m2_request")
    op.drop_index("ix_m2_audit_status", table_name="m2_audit")
    op.drop_table("m2_audit")
    op.drop_index("ix_m2_notification_status", table_name="m2_notification")
    op.drop_table("m2_notification")
    op.drop_index("ix_m2_seat_sync_status", table_name="m2_seat_sync")
    op.drop_index("ix_m2_seat_sync_application", table_name="m2_seat_sync")
    op.drop_table("m2_seat_sync")
    op.drop_index("ix_m2_approval_approver", table_name="m2_approval")
    op.drop_table("m2_approval")
    op.drop_index("ix_m2_application_space", table_name="m2_application")
    op.drop_index("ix_m2_application_applicant", table_name="m2_application")
    op.drop_index("ix_m2_application_status", table_name="m2_application")
    op.drop_table("m2_application")
    op.drop_table("m2_approval_config")
