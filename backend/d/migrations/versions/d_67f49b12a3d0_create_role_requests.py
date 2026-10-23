"""Persist successful D-S05 requests without altering existing roles or assignments."""
from alembic import op
import sqlalchemy as sa

revision = 'd_67f49b12a3d0'
down_revision = 'd_d34b70ea2c91'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('role_requests',
        sa.Column('request_id', sa.Text(), nullable=False),
        sa.Column('input_data', sa.JSON(), nullable=False),
        sa.Column('response_data', sa.JSON(), nullable=False),
        sa.Column('requested_by_user_id', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('length(trim(request_id)) > 0', name='ck_role_requests_id'),
        sa.CheckConstraint("jsonb_typeof(input_data::jsonb) = 'object'", name='ck_role_requests_input'),
        sa.CheckConstraint("jsonb_typeof(response_data::jsonb) = 'object'", name='ck_role_requests_response'),
        sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.user_id']),
        sa.PrimaryKeyConstraint('request_id'),
    )


def downgrade():
    op.drop_table('role_requests')
