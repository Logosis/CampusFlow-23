"""Add space ownership and persistent SYSTEM grants for D permission checks."""

from alembic import op
import sqlalchemy as sa


revision = "d_09c1a686d136"
down_revision = "d_3080cea5a2e0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "spaces",
        sa.Column("space_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("is_study_room", sa.Boolean(), nullable=False),
        sa.Column("owner_kind", sa.Text(), nullable=False),
        sa.Column("department_id", sa.Text(), nullable=True),
        sa.Column("registered", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "(owner_kind = 'SCHOOL' AND department_id IS NULL) OR "
            "(owner_kind = 'DEPARTMENT' AND department_id IS NOT NULL)",
            name="ck_spaces_owner",
        ),
        sa.CheckConstraint("version >= 1", name="ck_spaces_version"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_spaces_name"),
        sa.CheckConstraint("length(trim(location)) > 0", name="ck_spaces_location"),
        sa.ForeignKeyConstraint(["department_id"], ["departments.department_id"]),
        sa.PrimaryKeyConstraint("space_id"),
    )
    op.create_index("ix_spaces_department_id", "spaces", ["department_id"], unique=False)
    op.create_table(
        "system_subjects",
        sa.Column("module_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="ACTIVE", nullable=False),
        sa.CheckConstraint("module_id IN ('m1', 'm2', 'm3')", name="ck_system_subjects_module"),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_system_subjects_status"),
        sa.PrimaryKeyConstraint("module_id"),
    )
    op.create_table(
        "system_grants",
        sa.Column("grant_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("module_id", sa.Text(), nullable=False),
        sa.Column("permission_code", sa.Text(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(scope_type = 'GLOBAL' AND scope_id IS NULL) OR "
            "(scope_type IN ('DEPARTMENT', 'SPACE') "
            "AND scope_id IS NOT NULL AND length(trim(scope_id)) > 0)",
            name="ck_system_grants_scope",
        ),
        sa.ForeignKeyConstraint(["module_id"], ["system_subjects.module_id"]),
        sa.ForeignKeyConstraint(["permission_code"], ["permissions.code"]),
        sa.PrimaryKeyConstraint("grant_id"),
        sa.UniqueConstraint(
            "module_id", "permission_code", "scope_type", "scope_id",
            name="uq_system_grants_scope",
            postgresql_nulls_not_distinct=True,
        ),
    )


def downgrade():
    op.drop_table("system_grants")
    op.drop_table("system_subjects")
    op.drop_index("ix_spaces_department_id", table_name="spaces")
    op.drop_table("spaces")
