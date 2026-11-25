"""Distinguish a manual opening baseline from a monthly earned/released credit.

No ledger rows, account settings, historical values or active flags are rewritten.
Canonical W/H types become balance-tracked as required by the leave module.
"""
from alembic import op
import sqlalchemy as sa

revision = '0068_manual_opening_identity'
down_revision = '0067_sync_uid_base_entities'
branch_labels = None
depends_on = None

LEGACY = """(source_operation_key IS NULL AND source_digest IS NULL AND earning_year IS NULL AND released_on IS NULL) OR
(source_operation_key IS NOT NULL AND length(trim(source_operation_key)) > 0
 AND source_digest IS NOT NULL AND length(source_digest) = 64
 AND earning_year IS NOT NULL AND earning_year BETWEEN 1 AND 9999 AND released_on IS NOT NULL)"""
BASELINE = """(transaction_type = 'opening'
 AND source_operation_key ~ '^manual_opening:[0-9a-f]{64}:[AWH]$'
 AND source_digest ~ '^[0-9a-f]{64}$'
 AND earning_year IS NULL AND released_on IS NULL)"""


def upgrade():
    # NULL fields must never masquerade as monthly earned/released evidence.
    op.drop_constraint('ck_leave_ledger_source_identity', 'leave_balance_transactions', type_='check')
    op.create_check_constraint('ck_leave_ledger_source_identity', 'leave_balance_transactions',
                               '(' + LEGACY + ') OR COALESCE(' + BASELINE + ', false)')
    op.execute("UPDATE leave_types SET tracks_balance = true WHERE code IN ('weekly_rest','official_holiday') AND tracks_balance = false")


def downgrade():
    # Refuse before any DDL. Existing identity immutability remains enforced.
    if op.get_bind().execute(sa.text("SELECT count(*) FROM leave_balance_transactions WHERE source_operation_key IS NOT NULL AND earning_year IS NULL")).scalar_one():
        raise RuntimeError('Refusing downgrade: manual opening identity would be lost.')
    # No guessing which W/H types tracked balances before upgrade. The canonical
    # requirement is retained; downgrade never silently disables existing balances.
    op.drop_constraint('ck_leave_ledger_source_identity', 'leave_balance_transactions', type_='check')
    op.create_check_constraint('ck_leave_ledger_source_identity', 'leave_balance_transactions', LEGACY)
