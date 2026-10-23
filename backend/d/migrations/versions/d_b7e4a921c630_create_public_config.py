"""Persist public config and successful M1 config request results."""

from alembic import op
import sqlalchemy as sa


revision = "d_b7e4a921c630"
down_revision = "d_ac2072660bc7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "public_config",
        sa.Column("config_id", sa.Integer(), nullable=False, autoincrement=False),
        sa.Column("auth_method", sa.Text(), nullable=False),
        sa.Column("notification_channel", sa.Text(), nullable=False),
        sa.Column("notification_retry_limit", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("config_id = 1", name="ck_public_config_singleton"),
        sa.CheckConstraint("auth_method = 'PASSWORD'", name="ck_public_config_auth"),
        sa.CheckConstraint("notification_channel = 'EMAIL'", name="ck_public_config_channel"),
        sa.CheckConstraint("notification_retry_limit BETWEEN 0 AND 5", name="ck_public_config_retry"),
        sa.CheckConstraint("version >= 1", name="ck_public_config_version"),
        sa.PrimaryKeyConstraint("config_id"),
    )
    op.create_table(
        "public_config_requests",
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("input_data", sa.JSON(), nullable=False),
        sa.Column("response_data", sa.JSON(), nullable=False),
        sa.Column("requested_by_user_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(request_id)) > 0", name="ck_public_config_requests_id"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("request_id"),
    )


def downgrade():
    op.drop_table("public_config_requests")
    op.drop_table("public_config")
