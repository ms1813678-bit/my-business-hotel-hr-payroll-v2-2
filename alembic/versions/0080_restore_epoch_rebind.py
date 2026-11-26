"""Allow the same exact historical proposal once per restore epoch (fresh-proof rebind).

Old-epoch rows are never changed. Serialized rebind receipts are immutable audit rows
(action leave_opening_rebound) so no new table is needed.
"""
from alembic import op
import sqlalchemy as sa
revision = '0080_restore_epoch_rebind'
down_revision = '0079_leave_carry_expiry'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE leave_historical_evidence_preparations '
               'DROP CONSTRAINT leave_historical_evidence_preparations_proposal_sha256_key')
    op.execute("CREATE UNIQUE INDEX uq_leave_historical_proposal_epoch ON "
               "leave_historical_evidence_preparations (proposal_sha256, coalesce(restore_epoch, ''))")


def downgrade():
    dup = op.get_bind().execute(sa.text('SELECT count(*) FROM (SELECT proposal_sha256 FROM leave_historical_evidence_preparations '
                                        'GROUP BY proposal_sha256 HAVING count(*) > 1) d')).scalar_one()
    if dup:
        raise RuntimeError('Refusing downgrade: same proposal exists in several restore epochs.')
    op.execute('DROP INDEX uq_leave_historical_proposal_epoch')
    op.execute('ALTER TABLE leave_historical_evidence_preparations ADD CONSTRAINT '
               'leave_historical_evidence_preparations_proposal_sha256_key UNIQUE (proposal_sha256)')
