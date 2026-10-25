"""Add persistent seat master data for D-S06."""

from alembic import op
import sqlalchemy as sa


revision = "d_ac2072660bc7"
down_revision = "d_09c1a686d136"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "seats",
        sa.Column("seat_id", sa.Text(), nullable=False),
        sa.Column("seat_code", sa.Text(), nullable=False),
        sa.Column("space_id", sa.Text(), nullable=False),
        sa.Column("registered", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("socket_tags", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("length(trim(seat_id)) > 0", name="ck_seats_id"),
        sa.CheckConstraint("length(trim(seat_code)) > 0", name="ck_seats_code"),
        sa.CheckConstraint("version >= 1", name="ck_seats_version"),
        sa.CheckConstraint(
            "socket_tags::jsonb IN ('[]'::jsonb, '[\"FIXED\"]'::jsonb, "
            "'[\"RAIL\"]'::jsonb, '[\"FIXED\", \"RAIL\"]'::jsonb, "
            "'[\"RAIL\", \"FIXED\"]'::jsonb)",
            name="ck_seats_socket_tags",
        ),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.space_id"]),
        sa.PrimaryKeyConstraint("seat_id"),
        sa.UniqueConstraint("seat_code", name="uq_seats_seat_code"),
    )
    op.create_index("ix_seats_space_id", "seats", ["space_id"], unique=False)


def downgrade():
    op.drop_index("ix_seats_space_id", table_name="seats")
    op.drop_table("seats")
