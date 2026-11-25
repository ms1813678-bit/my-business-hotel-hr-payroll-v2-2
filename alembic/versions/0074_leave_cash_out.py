"""Immutable explicit manual leave cash-out receipts, no backfill.

One receipt = one reviewed liability/record + one exact ledger debit.
The receipt is never a payment: no paid state exists in this schema.
"""
import sqlalchemy as sa
from alembic import op
revision = '0074_leave_cash_out'
down_revision = '0073_leave_year_attribution'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_cash_outs',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('leave_type_id', sa.Integer(), sa.ForeignKey('leave_types.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('payroll_item_id', sa.Integer(), sa.ForeignKey('payroll_items.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('payroll_run_id', sa.Integer(), sa.ForeignKey('payroll_runs.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('evaluated_on', sa.Date(), nullable=False),
        sa.Column('days_canonical', sa.Text(), nullable=False),
        sa.Column('daily_rate_canonical', sa.Text(), nullable=False),
        sa.Column('cash_exact_canonical', sa.Text(), nullable=False),
        sa.Column('cash_egp', sa.Numeric(12, 2), nullable=False),
        sa.Column('basic_salary', sa.Numeric(12, 2), nullable=False),
        sa.Column('performance_bonus', sa.Numeric(12, 2), nullable=False),
        sa.Column('ledger_id', sa.Integer(), sa.ForeignKey('leave_balance_transactions.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('admin_accounts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('session_id', sa.String(128), nullable=False),
        sa.Column('session_version', sa.Integer(), nullable=False),
        sa.Column('restore_epoch', sa.String(128)),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('source_json', sa.Text(), nullable=False),
        sa.Column('result_json', sa.Text(), nullable=False),
        sa.Column('result_digest', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('ledger_id', name='uq_leave_cash_out_ledger'),
        sa.CheckConstraint('cash_egp > 0', name='ck_leave_cash_out_amount_positive'),
        sa.CheckConstraint('basic_salary >= 0 AND performance_bonus >= 0', name='ck_leave_cash_out_basis_nonnegative'))
    op.execute("""CREATE FUNCTION prevent_leave_cash_out_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'cash-out receipt is immutable'; END; $$ LANGUAGE plpgsql""")
    for command in ('UPDATE', 'DELETE', 'TRUNCATE'):
        op.execute(f'CREATE TRIGGER trg_leave_cash_out_{command.lower()} BEFORE {command} ON leave_cash_outs FOR EACH STATEMENT EXECUTE FUNCTION prevent_leave_cash_out_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_cash_outs')).scalar_one():
        raise RuntimeError('Refusing downgrade: immutable cash-out evidence exists.')
    op.drop_table('leave_cash_outs')
    op.execute('DROP FUNCTION prevent_leave_cash_out_mutation()')
