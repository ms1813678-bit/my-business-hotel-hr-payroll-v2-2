"""Late entries: remember the month a late bonus, deduction or advance payment was really meant for.

Additive only: nullable columns, no existing row changes. Rule (Mo's notes of 27 Nov 2026 23:40, 23:41 and
23:51, read in meaning, relayed by Main): an entry about month M is accepted for M until the 5th of the next
month; after that it goes into the current month with its reason and a "retroactive for M" label.
advance_requests.paid_date is the payment date HR entered (HR's claim, audited), paid_at stays the click time.
"""
from alembic import op
import sqlalchemy as sa

revision = '0085_late_entry_months'
down_revision = '0084_sync_change_log'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('staff_actions', sa.Column('retroactive_for_month', sa.Date(), nullable=True))
    op.create_check_constraint(
        'ck_staff_actions_retroactive_month', 'staff_actions',
        'retroactive_for_month IS NULL OR is_financial = TRUE')
    op.add_column('advance_requests', sa.Column('paid_date', sa.Date(), nullable=True))
    op.add_column('advance_requests', sa.Column('retroactive_for_month', sa.Date(), nullable=True))
    op.add_column('advance_requests', sa.Column('late_payment_reason', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('advance_requests', 'late_payment_reason')
    op.drop_column('advance_requests', 'retroactive_for_month')
    op.drop_column('advance_requests', 'paid_date')
    op.drop_constraint('ck_staff_actions_retroactive_month', 'staff_actions', type_='check')
    op.drop_column('staff_actions', 'retroactive_for_month')
