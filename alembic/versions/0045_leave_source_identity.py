"""Add nullable immutable-source identity fields to the existing leave ledger.

No existing row is relabeled, no balance is posted, no policy or route enabled.
Revision ID: 0045_leave_source_identity
Revises: 0044_leave_exact_day_storage
"""
import sqlalchemy as sa

from alembic import op

revision = '0045_leave_source_identity'
down_revision = '0044_leave_exact_day_storage'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('leave_balance_transactions', sa.Column('source_operation_key', sa.String(160), nullable=True))
    op.add_column('leave_balance_transactions', sa.Column('source_digest', sa.String(64), nullable=True))
    op.add_column('leave_balance_transactions', sa.Column('earning_year', sa.Integer(), nullable=True))
    op.add_column('leave_balance_transactions', sa.Column('released_on', sa.Date(), nullable=True))
    op.create_unique_constraint('uq_leave_ledger_source_operation', 'leave_balance_transactions', ['source_operation_key'])
    op.create_check_constraint('ck_leave_ledger_source_identity', 'leave_balance_transactions',
        "(source_operation_key IS NULL AND source_digest IS NULL AND earning_year IS NULL AND released_on IS NULL) OR "
        "(source_operation_key IS NOT NULL AND length(trim(source_operation_key)) > 0 "
        "AND source_digest IS NOT NULL AND length(source_digest) = 64 "
        "AND earning_year IS NOT NULL AND earning_year BETWEEN 1 AND 9999 AND released_on IS NOT NULL)")


def downgrade():
    connection = op.get_bind()
    count = connection.execute(sa.text('SELECT count(*) FROM leave_balance_transactions '
                                       'WHERE source_operation_key IS NOT NULL')).scalar_one()
    if count:
        raise RuntimeError('Refusing downgrade: source-bound leave history would lose identity.')
    op.drop_constraint('ck_leave_ledger_source_identity', 'leave_balance_transactions', type_='check')
    op.drop_constraint('uq_leave_ledger_source_operation', 'leave_balance_transactions', type_='unique')
    for name in ('released_on', 'earning_year', 'source_digest', 'source_operation_key'):
        op.drop_column('leave_balance_transactions', name)
