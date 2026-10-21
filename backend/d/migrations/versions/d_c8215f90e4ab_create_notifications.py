"""Persist notifications and per-recipient delivery state for D-S09/S10."""
from alembic import op
import sqlalchemy as sa

revision = "d_c8215f90e4ab"
down_revision = "d_b7e4a921c630"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notifications",
        sa.Column("notification_id", sa.Text(), nullable=False),
        sa.Column("source_module", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("business_id", sa.Text(), nullable=False),
        sa.Column("space_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_limit", sa.Integer(), nullable=False),
        sa.Column("input_data", sa.JSON(), nullable=False),
        sa.CheckConstraint('length(trim(notification_id)) > 0', name='ck_notifications_id'),
        sa.CheckConstraint('length(trim(request_id)) > 0', name='ck_notifications_request'),
        sa.CheckConstraint('length(trim(business_id)) > 0', name='ck_notifications_business'),
        sa.CheckConstraint("source_module IN ('M1', 'M2', 'M3')", name='ck_notifications_source'),
        sa.CheckConstraint("(source_module = 'M1' AND kind IN ('M1_BEFORE_START', 'M1_NOT_CHECKED_IN', 'M1_AUTO_CANCELLED', 'M1_MAINTENANCE_CANCELLED')) OR (source_module = 'M2' AND kind IN ('M2_APPROVAL_PENDING', 'M2_APPROVED', 'M2_REJECTED', 'M2_TIMEOUT_CANCELLED', 'M2_CANCELLED')) OR (source_module = 'M3' AND kind IN ('M3_ASSIGNED', 'M3_REPAIR_STARTED', 'M3_TIMEOUT_ESCALATED', 'M3_ACCEPTANCE_PENDING', 'M3_ACCEPTANCE_RESULT', 'M3_CANCELLED'))", name='ck_notifications_kind'),
        sa.CheckConstraint("channel = 'EMAIL'", name='ck_notifications_channel'),
        sa.CheckConstraint('length(trim(title)) > 0 AND length(title) <= 2000 AND position(chr(10) in title) = 0 AND position(chr(13) in title) = 0', name='ck_notifications_title'),
        sa.CheckConstraint('length(trim(body)) > 0 AND length(body) <= 2000', name='ck_notifications_body'),
        sa.CheckConstraint('retry_limit BETWEEN 0 AND 5', name='ck_notifications_retry'),
        sa.CheckConstraint("jsonb_typeof(input_data::jsonb) = 'object'", name='ck_notifications_input'),
        sa.UniqueConstraint("source_module", "request_id", name="uq_notifications_source_request"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.space_id"]),
        sa.PrimaryKeyConstraint("notification_id"),
    )
    op.create_index("ix_notifications_space_id", "notifications", ["space_id"])
    op.create_table(
        "notification_deliveries",
        sa.Column("notification_id", sa.Text(), nullable=False),
        sa.Column("recipient_user_id", sa.Text(), nullable=False),
        sa.Column("recipient_order", sa.Integer(), nullable=False),
        sa.Column("recipient_email", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_token", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint('recipient_order >= 0', name='ck_notification_deliveries_order'),
        sa.CheckConstraint("status IN ('PENDING', 'SENDING', 'SUBMITTED', 'FAILED', 'UNKNOWN')", name='ck_notification_deliveries_status'),
        sa.CheckConstraint('attempt_count BETWEEN 0 AND 6', name='ck_notification_deliveries_count'),
        sa.CheckConstraint('(attempt_count = 0 AND last_attempt_at IS NULL) OR (attempt_count > 0 AND last_attempt_at IS NOT NULL)', name='ck_notification_deliveries_attempt'),
        sa.CheckConstraint("status NOT IN ('SENDING', 'SUBMITTED', 'UNKNOWN') OR attempt_count > 0", name='ck_notification_deliveries_attempted_status'),
        sa.CheckConstraint("(status = 'PENDING' AND next_attempt_at IS NOT NULL) OR (status <> 'PENDING' AND next_attempt_at IS NULL)", name='ck_notification_deliveries_next'),
        sa.CheckConstraint("(status = 'SENDING' AND attempt_token IS NOT NULL AND length(trim(attempt_token)) > 0 AND lease_expires_at IS NOT NULL AND lease_expires_at > last_attempt_at) OR (status <> 'SENDING' AND attempt_token IS NULL AND lease_expires_at IS NULL)", name='ck_notification_deliveries_lease'),
        sa.CheckConstraint('recipient_email IS NULL OR (length(trim(recipient_email)) > 0 AND position(chr(10) in recipient_email) = 0 AND position(chr(13) in recipient_email) = 0)', name='ck_notification_deliveries_email'),
        sa.CheckConstraint("status NOT IN ('SENDING', 'SUBMITTED', 'UNKNOWN') OR recipient_email IS NOT NULL", name='ck_notification_deliveries_address'),
        sa.CheckConstraint('length(trim(reason)) > 0', name='ck_notification_deliveries_reason'),
        sa.UniqueConstraint("notification_id", "recipient_order", name="uq_notification_deliveries_order"),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.notification_id"]),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("notification_id", "recipient_user_id"),
    )
    op.create_index("ix_notification_deliveries_due", "notification_deliveries", ["status", "next_attempt_at"])
    op.create_index("ix_notification_deliveries_lease", "notification_deliveries", ["status", "lease_expires_at"])


def downgrade():
    op.drop_index("ix_notification_deliveries_lease", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_due", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_index("ix_notifications_space_id", table_name="notifications")
    op.drop_table("notifications")
