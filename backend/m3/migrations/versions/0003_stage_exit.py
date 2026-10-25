"""Record stage exit times so statistics do not depend on escalation polling."""
from alembic import op
import sqlalchemy as sa

revision = 'm3_0003'
down_revision = 'm3_0002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('m3_stage_cycles', sa.Column('exited_at', sa.DateTime(timezone=True)))
    # Preserve existing stage history when upgrading an already used M3 database.
    op.execute("""
        WITH entered AS (
            SELECT work_order_id, version,
                CASE kind WHEN 'REPORT' THEN 'WAITING_ASSIGNMENT'
                          WHEN 'DISPATCH' THEN 'ASSIGNED'
                          WHEN 'REPAIR' THEN 'REPAIRING'
                          WHEN 'RETURN' THEN 'REPAIRING'
                          WHEN 'COMPLETE' THEN 'WAITING_ACCEPTANCE' END AS stage,
                row_number() OVER (
                    PARTITION BY work_order_id,
                        CASE kind WHEN 'REPAIR' THEN 'REPAIRING'
                                  WHEN 'RETURN' THEN 'REPAIRING' ELSE kind END
                    ORDER BY version, id) AS cycle
            FROM m3_records WHERE kind IN ('REPORT', 'DISPATCH', 'REPAIR', 'RETURN', 'COMPLETE')
        )
        UPDATE m3_stage_cycles AS timer
        SET exited_at = (
            SELECT event.occurred_at FROM m3_records AS event
            WHERE event.work_order_id = timer.work_order_id AND event.version > entered.version
              AND event.kind IN ('DISPATCH', 'REPAIR', 'RETURN', 'COMPLETE', 'ACCEPT', 'CANCEL')
            ORDER BY event.version, event.id LIMIT 1)
        FROM entered WHERE timer.work_order_id = entered.work_order_id
            AND timer.stage = entered.stage AND timer.cycle = entered.cycle
    """)


def downgrade():
    op.drop_column('m3_stage_cycles', 'exited_at')
