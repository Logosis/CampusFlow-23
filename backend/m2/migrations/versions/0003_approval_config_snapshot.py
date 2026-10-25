"""Preserve submitted approval rules; backfill only a provably matching version."""
from alembic import op
import sqlalchemy as sa

revision = "m2_0003"
down_revision = "m2_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("m2_application", sa.Column("approval_config_snapshot", sa.JSON(), nullable=True))
    bind = op.get_bind()
    # Offline output cannot inspect legacy data; actual migrations perform safe backfill.
    if op.get_context().as_sql:
        return
    config = sa.table("m2_approval_config", sa.column("id", sa.Integer()), sa.column("version", sa.Integer()),
        sa.column("application_scopes", sa.JSON()), sa.column("approval_levels", sa.JSON()),
        sa.column("clock_start", sa.String()), sa.column("timeout_seconds", sa.Integer()),
        sa.column("check_interval_seconds", sa.Integer()))
    applications = sa.table("m2_application", sa.column("config_version", sa.Integer()),
        sa.column("approval_config_snapshot", sa.JSON()))
    current = bind.execute(sa.select(config).where(config.c.id == 1)).mappings().first()
    if current:
        snapshot = {"version": current["version"], "applicationScopes": current["application_scopes"],
            "approvalLevels": current["approval_levels"], "clockStart": current["clock_start"],
            "timeoutSeconds": current["timeout_seconds"], "checkIntervalSeconds": current["check_interval_seconds"]}
        bind.execute(applications.update().where(applications.c.config_version == current["version"])
                     .values(approval_config_snapshot=snapshot))


def downgrade():
    op.drop_column("m2_application", "approval_config_snapshot")
