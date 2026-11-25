"""Populate exactly one cash-out receipt on a scratch migrated database.

Run by test_leave_cash_out_migration_pg.py against a throwaway database whose
DATABASE_URL is set by the wrapper. Synthetic data only.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.leave_cash_out import apply_cash_out, review_cash_out
from app.models import AdminAccount, AdminSession
from app.restore_lock import read_restore_epoch
from tests.cash_out_http_acceptance import seed


def test_populate_one_cash_out_receipt():
    employee_id, type_id, item_id = seed()
    with SessionLocal() as db:
        account = db.scalar(select(AdminAccount).where(AdminAccount.username == 'synthetic-cashout-hr'))
        session = db.scalar(select(AdminSession).where(AdminSession.admin_account_id == account.id))
        auth = {'actor_id': account.id, 'session_id': session.id,
                'session_version': account.session_version, 'expected_epoch': read_restore_epoch(db)}
        args = dict(employee_id=employee_id, leave_type_id=type_id, days=Decimal(1),
                    payroll_item_id=item_id, evaluated_on=date(2026, 4, 2), **auth)
        review = review_cash_out(db, **args)
        result = apply_cash_out(db, operation_id='9' * 64, reviewed_digest=review['reviewed_digest'],
                                human_confirmed=True, reason='Synthetic migration cash-out', **args)
        assert result['cash_egp'] == '120.00'
        db.commit()
