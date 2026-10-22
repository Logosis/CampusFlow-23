"""Create M1 reservation, availability, source-limit, and outbox tables."""

from alembic import op
import sqlalchemy as sa


revision = "m1_0002"
down_revision = "m1_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "m1_config",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("max_reservation_hours", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "m1_opening_schedule",
        sa.Column("space_id", sa.String(128), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "m1_opening_range",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("space_id", sa.String(128), sa.ForeignKey("m1_opening_schedule.space_id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_m1_opening_space_time", "m1_opening_range", ["space_id", "start_time", "end_time"])
    op.create_table(
        "m1_reservation",
        sa.Column("reservation_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("space_id", sa.String(128), nullable=False),
        sa.Column("seat_id", sa.String(128), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("check_in_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("before_start_notified", sa.Boolean(), nullable=False),
        sa.Column("not_checked_in_notified", sa.Boolean(), nullable=False),
        sa.Column("auto_cancel_notified", sa.Boolean(), nullable=False),
        sa.Column("maintenance_cancel_notified", sa.Boolean(), nullable=False),
        sa.Column("violation_recorded", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_m1_reservation_seat_time", "m1_reservation", ["seat_id", "start_time", "end_time"])
    op.create_index("ix_m1_reservation_user_time", "m1_reservation", ["user_id", "start_time"])
    op.create_index("ix_m1_reservation_space_status", "m1_reservation", ["space_id", "status"])
    op.create_table(
        "m1_check_in_code",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("space_id", sa.String(128), nullable=False),
        sa.Column("campus_date", sa.String(10), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("space_id", "campus_date", name="uq_m1_code_space_date"),
    )
    op.create_table(
        "m1_violation",
        sa.Column("violation_id", sa.String(64), primary_key=True),
        sa.Column("reservation_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.UniqueConstraint("reservation_id", name="uq_m1_violation_reservation"),
    )
    op.create_table(
        "m1_idempotency",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("function_name", sa.String(64), nullable=False),
        sa.Column("actor_key", sa.String(160), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("function_name", "actor_key", "request_id", name="uq_m1_idempotency"),
    )
    op.create_table(
        "m1_room_limit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("business_id", sa.String(128), nullable=False),
        sa.Column("space_id", sa.String(128), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(16), nullable=True),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column("max_processed_version", sa.Integer(), nullable=False),
        sa.Column("last_payload_hash", sa.String(64), nullable=False),
        sa.Column("last_status", sa.String(16), nullable=False),
        sa.Column("last_message", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "business_id", name="uq_m1_limit_business"),
    )
    op.create_index("ix_m1_limit_space_state", "m1_room_limit", ["space_id", "state"])
    op.create_table(
        "m1_room_limit_request",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("business_id", sa.String(128), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "request_id", name="uq_m1_limit_request"),
    )
    op.create_index("ix_m1_limit_request_business", "m1_room_limit_request", ["source", "business_id"])
    op.create_table(
        "m1_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("kind", "request_id", name="uq_m1_outbox_request"),
    )
    op.create_table(
        "m1_management_lock",
        sa.Column("resource_key", sa.String(160), primary_key=True),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("m1_management_lock")
    op.drop_table("m1_outbox")
    op.drop_index("ix_m1_limit_request_business", table_name="m1_room_limit_request")
    op.drop_table("m1_room_limit_request")
    op.drop_index("ix_m1_limit_space_state", table_name="m1_room_limit")
    op.drop_table("m1_room_limit")
    op.drop_table("m1_idempotency")
    op.drop_table("m1_violation")
    op.drop_table("m1_check_in_code")
    op.drop_index("ix_m1_reservation_space_status", table_name="m1_reservation")
    op.drop_index("ix_m1_reservation_user_time", table_name="m1_reservation")
    op.drop_index("ix_m1_reservation_seat_time", table_name="m1_reservation")
    op.drop_table("m1_reservation")
    op.drop_index("ix_m1_opening_space_time", table_name="m1_opening_range")
    op.drop_table("m1_opening_range")
    op.drop_table("m1_opening_schedule")
    op.drop_table("m1_config")
