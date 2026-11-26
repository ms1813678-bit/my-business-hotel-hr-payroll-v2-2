"""Separate payslip line for leave cash-out (owner decision Nov 26): payroll_items.leave_cash_amount."""
from alembic import op
import sqlalchemy as sa
revision = '0081_payroll_leave_cash_line'
down_revision = '0080_restore_epoch_rebind'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('payroll_items', sa.Column('leave_cash_amount', sa.Numeric(12, 2), nullable=False, server_default='0'))
    op.create_check_constraint('ck_payroll_item_leave_cash_nonneg', 'payroll_items', 'leave_cash_amount >= 0')


def downgrade():
    bad = op.get_bind().execute(sa.text('SELECT count(*) FROM payroll_items WHERE leave_cash_amount <> 0')).scalar_one()
    if bad:
        raise RuntimeError('Refusing downgrade: payroll items carry leave cash amounts.')
    op.drop_constraint('ck_payroll_item_leave_cash_nonneg', 'payroll_items')
    op.drop_column('payroll_items', 'leave_cash_amount')
