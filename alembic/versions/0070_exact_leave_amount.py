"""One ledger, exclusive finite decimal OR reduced rational amount.

No rows are rewritten and no accrual/release is posted by this migration.
"""
import sqlalchemy as sa
from alembic import op

revision = '0070_exact_leave_amount'
down_revision = '0069_leave_register_consume'
branch_labels = None
depends_on = None

EXACT_CHECK = """(days_delta IS NOT NULL AND exact_numerator IS NULL AND exact_denominator IS NULL) OR
(days_delta IS NULL AND exact_numerator IS NOT NULL AND exact_denominator IS NOT NULL
 AND exact_numerator <> 0 AND exact_denominator > 1
 AND exact_numerator = trunc(exact_numerator) AND exact_denominator = trunc(exact_denominator)
 AND length(exact_numerator::text) <= 32 AND length(exact_denominator::text) <= 32
 AND exact_numerator::text NOT IN ('NaN','Infinity','-Infinity')
 AND exact_denominator::text NOT IN ('NaN','Infinity','-Infinity')
 AND gcd(exact_numerator, exact_denominator) = 1)"""


def upgrade():
    op.add_column('leave_balance_transactions', sa.Column('exact_numerator', sa.Numeric(), nullable=True))
    op.add_column('leave_balance_transactions', sa.Column('exact_denominator', sa.Numeric(), nullable=True))
    op.alter_column('leave_balance_transactions', 'days_delta', existing_type=sa.Numeric(), nullable=True)
    op.create_check_constraint('ck_leave_ledger_exact_amount','leave_balance_transactions', EXACT_CHECK)


def downgrade():
    count = op.get_bind().execute(sa.text('SELECT count(*) FROM leave_balance_transactions WHERE days_delta IS NULL OR exact_numerator IS NOT NULL OR exact_denominator IS NOT NULL')).scalar_one()
    if count:
        raise RuntimeError('Refusing exact amount downgrade: rational evidence would be lost.')
    op.drop_constraint('ck_leave_ledger_exact_amount','leave_balance_transactions',type_='check')
    op.alter_column('leave_balance_transactions','days_delta',existing_type=sa.Numeric(),nullable=False)
    op.drop_column('leave_balance_transactions','exact_denominator')
    op.drop_column('leave_balance_transactions','exact_numerator')
