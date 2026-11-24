"""Preserve exact leave day deltas without a second balance ledger.

No values/transaction kinds/routes are changed. Old leave helpers remain as-is;
new integration must use an explicit exact-days reader. A downgrade refuses any
value that would round or overflow rather than silently destroying precision.

Revision ID: 0044_leave_exact_day_storage
Revises: 0043_leave_close_metadata
"""
import sqlalchemy as sa

from alembic import op

revision = '0044_leave_exact_day_storage'
down_revision = '0043_leave_close_metadata'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('leave_balance_transactions', 'days_delta',
                    existing_type=sa.Numeric(7, 2), type_=sa.Numeric(), existing_nullable=False)


def downgrade() -> None:
    connection = op.get_bind()
    unsafe = connection.execute(sa.text('''
        SELECT count(*) FROM leave_balance_transactions
        WHERE days_delta <> round(days_delta, 2) OR abs(days_delta) >= 100000
    ''')).scalar_one()
    if unsafe:
        raise RuntimeError('Refusing leave precision downgrade: existing values would round or overflow.')
    op.alter_column('leave_balance_transactions', 'days_delta',
                    existing_type=sa.Numeric(), type_=sa.Numeric(7, 2), existing_nullable=False)
