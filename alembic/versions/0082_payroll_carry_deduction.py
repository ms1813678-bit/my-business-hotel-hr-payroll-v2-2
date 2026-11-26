"""Payroll carry of excess deductions: two exact amounts on each payroll item.

Carry design (relayed via Main; this docstring is not an approval record): when a month's
deductions exceed its earnings, net pay is 0 and the excess is carried to the same employee's next
month as a separate deduction line. carried_in_deduction is that incoming line; carried_out_amount is
the excess this item could not absorb. Existing rows get 0 for both (no past month changes).
"""
from alembic import op
import sqlalchemy as sa

revision = '0082_payroll_carry_deduction'
down_revision = '0081_payroll_leave_cash_line'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('carried_in_deduction', 'carried_out_amount'):
        op.add_column('payroll_items', sa.Column(name, sa.Numeric(12, 2), nullable=False, server_default='0'))
    op.create_check_constraint('ck_payroll_items_carry_nonnegative', 'payroll_items',
                               'carried_in_deduction >= 0 AND carried_out_amount >= 0')


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text('SELECT count(*) FROM payroll_items WHERE carried_in_deduction <> 0 OR carried_out_amount <> 0')).scalar_one():
        raise RuntimeError('Refusing downgrade: payroll items carry deductions between months.')
    op.drop_constraint('ck_payroll_items_carry_nonnegative', 'payroll_items', type_='check')
    op.drop_column('payroll_items', 'carried_out_amount')
    op.drop_column('payroll_items', 'carried_in_deduction')
