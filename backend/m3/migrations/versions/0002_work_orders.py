"""M3 work orders, immutable records, timers, idempotency receipts and durable outbox."""
from alembic import op
import sqlalchemy as sa

revision = 'm3_0002'
down_revision = 'm3_0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('m3_work_orders',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('reporter_id', sa.Text(), nullable=False),
        sa.Column('space_id', sa.Text(), nullable=False),
        sa.Column('department_id', sa.Text()),
        sa.Column('fault_location', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('assignee_id', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('stage_entered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('first_repair_started_at', sa.DateTime(timezone=True)),
        sa.Column('accepted_at', sa.DateTime(timezone=True)),
        sa.Column('timeout_config_version', sa.Integer()))
    for col in ('space_id', 'status', 'created_at'):
        op.create_index(f'ix_m3_work_orders_{col}', 'm3_work_orders', [col])
    op.create_table('m3_records',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('work_order_id', sa.String(64), sa.ForeignKey('m3_work_orders.id'), nullable=False),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('actor_id', sa.Text()),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False))
    op.create_index('ix_m3_records_work_order_id', 'm3_records', ['work_order_id'])
    op.create_table('m3_stage_cycles',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('work_order_id', sa.String(64), sa.ForeignKey('m3_work_orders.id'), nullable=False),
        sa.Column('stage', sa.String(32), nullable=False),
        sa.Column('cycle', sa.Integer(), nullable=False),
        sa.Column('config_version', sa.Integer(), nullable=False),
        sa.Column('rule', sa.JSON()),
        sa.Column('due_at', sa.DateTime(timezone=True)),
        sa.Column('entered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('escalation_id', sa.String(64)),
        sa.UniqueConstraint('work_order_id', 'stage', 'cycle'))
    op.create_index('ix_m3_stage_cycles_work_order_id', 'm3_stage_cycles', ['work_order_id'])
    op.create_table('m3_outbox',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('work_order_id', sa.String(64), sa.ForeignKey('m3_work_orders.id')),
        sa.Column('record_id', sa.String(64)),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('request_id', sa.String(128), nullable=False, unique=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('selector', sa.JSON()),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('result', sa.JSON()),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True)),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('attempts', sa.Integer(), nullable=False))
    op.create_index('ix_m3_outbox_work_order_id', 'm3_outbox', ['work_order_id'])
    op.create_table('m3_receipts',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('owner', sa.Text(), nullable=False),
        sa.Column('function', sa.String(64), nullable=False),
        sa.Column('request_id', sa.Text(), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('work_order_id', sa.String(64)),
        sa.Column('response', sa.JSON(), nullable=False),
        sa.UniqueConstraint('owner', 'function', 'request_id'))
    op.create_table('m3_timeout_config',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('data', sa.JSON(), nullable=False))
    op.create_table('m3_worker_state',
        sa.Column('name', sa.String(32), primary_key=True),
        sa.Column('last_run', sa.DateTime(timezone=True)))


def downgrade():
    for table in ('m3_worker_state', 'm3_timeout_config', 'm3_receipts', 'm3_outbox',
                  'm3_stage_cycles', 'm3_records', 'm3_work_orders'):
        op.drop_table(table)
