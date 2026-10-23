"""S07 successful receipts and permanent seat-code reservations; preserve existing rows."""
from alembic import op
import sqlalchemy as sa

revision = 'd_b7a26ef195c4'
down_revision = 'd_67f49b12a3d0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('master_requests',
        sa.Column('request_id', sa.Text(), nullable=False),
        sa.Column('input_data', sa.JSON(), nullable=False),
        sa.Column('response_data', sa.JSON(), nullable=False),
        sa.Column('requested_by_user_id', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('length(trim(request_id)) > 0', name='ck_master_requests_id'),
        sa.CheckConstraint("jsonb_typeof(input_data::jsonb) = 'object'", name='ck_master_requests_input'),
        sa.CheckConstraint("jsonb_typeof(response_data::jsonb) = 'object'", name='ck_master_requests_response'),
        sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.user_id']),
        sa.PrimaryKeyConstraint('request_id'),
    )
    # 这是一份永久占号集合，不随座位记录或当前编号改变而删除。
    op.create_table('seat_code_history',
        sa.Column('seat_code', sa.Text(), nullable=False),
        sa.CheckConstraint('length(trim(seat_code)) > 0', name='ck_seat_code_history_code'),
        sa.PrimaryKeyConstraint('seat_code'),
    )
    op.execute('INSERT INTO seat_code_history (seat_code) SELECT seat_code FROM seats')


def downgrade():
    op.drop_table('seat_code_history')
    op.drop_table('master_requests')
