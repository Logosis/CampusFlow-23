"""Add persistent D-S11 audit events, preserving all existing service data."""
from alembic import op
import sqlalchemy as sa

revision = 'd_d34b70ea2c91'
down_revision = 'd_c8215f90e4ab'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'audit_events',
        sa.Column('audit_id', sa.Text(), nullable=False),
        sa.Column('source_module', sa.Text(), nullable=False),
        sa.Column('request_id', sa.Text(), nullable=False),
        sa.Column('actor_type', sa.Text(), nullable=False),
        sa.Column('actor_user_id', sa.Text(), nullable=True),
        sa.Column('object_type', sa.Text(), nullable=False),
        sa.Column('business_id', sa.Text(), nullable=False),
        sa.Column('space_id', sa.Text(), nullable=True),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('result', sa.Text(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('verified_by', sa.Text(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('input_data', sa.JSON(), nullable=False),
        sa.CheckConstraint('length(trim(audit_id)) > 0', name='ck_audit_events_id'),
        sa.CheckConstraint('length(trim(request_id)) > 0', name='ck_audit_events_request'),
        sa.CheckConstraint("source_module IN ('M1','M2','M3','D')", name='ck_audit_events_source'),
        sa.CheckConstraint("actor_type IN ('USER','SYSTEM','UNKNOWN')", name='ck_audit_events_actor'),
        sa.CheckConstraint("(actor_type = 'USER' AND actor_user_id IS NOT NULL) OR (actor_type <> 'USER' AND actor_user_id IS NULL)", name='ck_audit_events_actor_user'),
        sa.CheckConstraint('length(trim(object_type)) > 0 AND length(object_type) <= 2000', name='ck_audit_events_object'),
        sa.CheckConstraint('length(trim(business_id)) > 0', name='ck_audit_events_business'),
        sa.CheckConstraint('length(trim(action)) > 0 AND length(action) <= 2000', name='ck_audit_events_action'),
        sa.CheckConstraint("result IN ('SUCCESS','REJECTED','FAILED','UNKNOWN')", name='ck_audit_events_result'),
        sa.CheckConstraint('length(trim(summary)) > 0 AND length(summary) <= 2000', name='ck_audit_events_summary'),
        sa.CheckConstraint("verified_by = source_module OR (verified_by = 'D' AND actor_type = 'USER')", name='ck_audit_events_verified'),
        sa.CheckConstraint("jsonb_typeof(input_data::jsonb) = 'object'", name='ck_audit_events_input'),
        sa.UniqueConstraint('source_module', 'request_id', name='uq_audit_events_source_request'),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.user_id']),
        sa.ForeignKeyConstraint(['space_id'], ['spaces.space_id']),
        sa.PrimaryKeyConstraint('audit_id'),
    )
    op.create_index('ix_audit_events_time_id', 'audit_events', ['occurred_at', 'audit_id'])
    op.create_index('ix_audit_events_source_time', 'audit_events', ['source_module', 'occurred_at', 'audit_id'])
    op.create_index('ix_audit_events_business', 'audit_events', ['business_id'])
    op.create_index('ix_audit_events_actor_user_id', 'audit_events', ['actor_user_id'])
    op.create_index('ix_audit_events_space_id', 'audit_events', ['space_id'])


def downgrade():
    for name in ('ix_audit_events_space_id', 'ix_audit_events_actor_user_id', 'ix_audit_events_business',
                 'ix_audit_events_source_time', 'ix_audit_events_time_id'):
        op.drop_index(name, table_name='audit_events')
    op.drop_table('audit_events')
