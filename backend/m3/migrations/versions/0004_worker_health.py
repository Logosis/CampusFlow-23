"""Keep compensation failures visible between runs and across process restarts."""
from alembic import op
import sqlalchemy as sa

revision = 'm3_0004'
down_revision = 'm3_0003'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('m3_worker_state', sa.Column('failed', sa.Boolean(), nullable=False,
                                            server_default=sa.false()))
    # An old run has no reliable persisted outcome. Reconcile it before declaring recovery.
    op.execute("UPDATE m3_worker_state SET failed = true WHERE name = 'compensation'")


def downgrade():
    op.drop_column('m3_worker_state', 'failed')
