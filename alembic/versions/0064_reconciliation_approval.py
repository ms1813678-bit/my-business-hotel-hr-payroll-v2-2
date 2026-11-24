"""Explicit immutable HR approval, no balance/financial effects or backfill."""
from alembic import op
import sqlalchemy as sa
revision='0064_reconciliation_approval'
down_revision='0063_reconciliation_review'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('leave_reconciliation_approvals',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('receipt_id',sa.String(64),sa.ForeignKey('leave_reconciliation_review_receipts.id'),nullable=False,unique=True),
        sa.Column('predecessor_id',sa.String(64),sa.ForeignKey('leave_reconciliation_approvals.id'),unique=True),
        sa.Column('scope_key',sa.String(64),nullable=False),
        sa.Column('employee_id',sa.Integer(),sa.ForeignKey('employees.id'),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_accounts.id'),nullable=False),
        sa.Column('session_id',sa.String(128),nullable=False),
        sa.Column('session_version',sa.Integer(),nullable=False),
        sa.Column('restore_epoch',sa.Text()),
        sa.Column('source_digest',sa.String(64),nullable=False),
        sa.Column('decision_digest',sa.String(64),nullable=False),
        sa.Column('comparison_digest',sa.String(64),nullable=False),
        sa.Column('request_digest',sa.String(64),nullable=False),
        sa.Column('reason',sa.Text(),nullable=False),
        sa.Column('integrity_digest',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_leave_reconciliation_approvals_scope_key','leave_reconciliation_approvals',['scope_key'])
    op.create_index('uq_reconciliation_approval_baseline','leave_reconciliation_approvals',['scope_key'],unique=True,postgresql_where=sa.text('predecessor_id IS NULL'))
    op.execute('CREATE TRIGGER trg_leave_reconciliation_approval_immutable BEFORE UPDATE OR DELETE ON leave_reconciliation_approvals FOR EACH ROW EXECUTE FUNCTION refuse_synthetic_transition_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_reconciliation_approvals')).scalar_one():
        raise RuntimeError('Refusing downgrade: HR approval history would be lost.')
    op.drop_table('leave_reconciliation_approvals')
