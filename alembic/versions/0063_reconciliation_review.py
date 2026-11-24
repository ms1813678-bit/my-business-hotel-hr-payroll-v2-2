"""Durable assertion-only reconciliation review. No backfill or balance activation."""
from alembic import op
import sqlalchemy as sa
revision='0063_reconciliation_review'
down_revision='0062_synthetic_register_consume'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('leave_reconciliation_review_receipts',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('comparison_id',sa.String(64),sa.ForeignKey('department_fingerprint_preparations.id'),nullable=False),
        sa.Column('employee_id',sa.Integer(),sa.ForeignKey('employees.id'),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_accounts.id'),nullable=False),
        sa.Column('session_id',sa.String(128),nullable=False),
        sa.Column('session_version',sa.Integer(),nullable=False),
        sa.Column('restore_epoch',sa.Text()),
        sa.Column('source_digest',sa.String(64),nullable=False),
        sa.Column('decision_digest',sa.String(64),nullable=False),
        sa.Column('assertions_json',sa.Text(),nullable=False),
        sa.Column('reason',sa.Text(),nullable=False),
        sa.Column('integrity_digest',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.execute('CREATE TRIGGER trg_leave_reconciliation_review_immutable BEFORE UPDATE OR DELETE ON leave_reconciliation_review_receipts FOR EACH ROW EXECUTE FUNCTION refuse_synthetic_transition_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_reconciliation_review_receipts')).scalar_one():
        raise RuntimeError('Refusing downgrade: recorded operator assertions would be lost.')
    op.drop_table('leave_reconciliation_review_receipts')
