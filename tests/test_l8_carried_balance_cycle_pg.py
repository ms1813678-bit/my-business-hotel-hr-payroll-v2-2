# ruff: noqa: F811
"""Item 2b full-cycle acceptance on real PostgreSQL, through the real services (no monkeypatched consumers):
a 2025 weekly-rest carry grant is partly used in 2026 (attribution 9/7); cash-out is refused while the expired grant is
unresolved; the explicit expiry is refused until the remaining in-window usage is attributed (the first real block of
the cycle, pinned, no writes); a manual adjustment moves the balance by exactly its amount. Ledger, receipts and
attribution agree at every step. Fixed clock 2027-01-15, py3.10 shim, not 3.11. Values are exact
fractions of days from a synthetic employee. No new rule: owner rules are no netting, no FIFO, manual cash-out any time."""
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlalchemy import select

from app.leave_carry_expiry import review_carry_expiry
from app.leave_cash_out import review_cash_out
from app.leave_exact_amount import as_fraction, ledger_amount
from app.leave_workflow import add_balance_entry, leave_balance
from app.models import (LeaveBalanceTransaction, LeaveCarryAllocation, LeaveCarryConsumption, LeaveCarryExpiry,
                        LeaveCashOut, LeaveType)
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_net_source import FULL, IDENT, Clock, full_year
from tests.test_leave_cash_out import payroll


def ledger_sum(db, e, kind):
    rows = db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id == e,
        LeaveBalanceTransaction.leave_type_id == kind)).all()
    return sum((as_fraction(ledger_amount(r)) for r in rows), Fraction())


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_carried_weekly_balance_used_expired_cashed_and_adjusted_stays_consistent(prior_year, monkeypatch):
    monkeypatch.setattr('app.leave_cash_out.datetime', Clock)
    monkeypatch.setattr('app.leave_carry_expiry.datetime', Clock)
    db, args = full_year(prior_year, monkeypatch)
    e, kind = args['employee_id'], args['leave_type_id']
    auth = {k: args[k] for k in IDENT}
    # 1. start: balance is the ledger sum; the 2026 usage is attributed to the 2025 grant (1 + 2/7 = 9/7)
    start = leave_balance(db, e, kind)
    assert start == ledger_sum(db, e, kind)
    attributed = db.scalars(select(LeaveCarryConsumption).where(LeaveCarryConsumption.employee_id == e)).all()
    assert sum((Fraction(c.quantity_canonical) for c in attributed), Fraction()) == Fraction(9, 7)
    grant = db.scalar(select(LeaveCarryAllocation).where(LeaveCarryAllocation.employee_id == e,
        LeaveCarryAllocation.leave_type_id == kind, LeaveCarryAllocation.source_year == 2025))
    assert grant is not None
    _run, item = payroll(db, prior_year[1], start=date(2026, 12, 1), end=date(2026, 12, 31), status='approved')
    cash_args = dict(employee_id=e, leave_type_id=kind, days='1/7', payroll_item_id=item.id,
                     evaluated_on=date(2027, 1, 10), **auth)
    cash_before = len(db.scalars(select(LeaveCashOut).where(LeaveCashOut.employee_id == e)).all())   # 2025 settlement cash
    # 2. cash is refused while the expired carry is unresolved, with no write
    n_ledger = db.scalar(select(__import__('sqlalchemy').func.count()).select_from(LeaveBalanceTransaction))
    with pytest.raises(ValueError, match='Expired carry remains unresolved'):
        review_cash_out(db, **cash_args)
    # 3. FIRST REAL BLOCK of the cycle: the explicit expiry of that grant is refused while other 2026 register usage inside
    #    the grant window is not yet attributed to a grant (no FIFO: HR must attribute each day). Nothing is written.
    ex = dict(grant_kind='carry', grant_id=grant.id, employee_id=e, leave_type_id=kind, evaluated_on=date(2027, 1, 2),
              reason='Synthetic explicit old weekly carry expiry', **auth)
    with pytest.raises(ValueError, match='Unallocated register usage could already consume this grant'):
        review_carry_expiry(db, **ex)
    assert db.scalar(select(__import__('sqlalchemy').func.count()).select_from(LeaveBalanceTransaction)) == n_ledger
    assert db.scalars(select(LeaveCarryExpiry).where(LeaveCarryExpiry.employee_id == e)).all() == []
    assert len(db.scalars(select(LeaveCashOut).where(LeaveCashOut.employee_id == e)).all()) == cash_before
    assert leave_balance(db, e, kind) == start
    # 5. manual adjustment through the legacy writer: balance moves by exactly that amount, no other receipt changes
    add_balance_entry(db, employee_id=e, leave_type=db.get(LeaveType, kind), transaction_type='adjustment',
                      days_delta=Decimal(2), reason='Synthetic manual correction', actor='synthetic-workbook-store')
    assert leave_balance(db, e, kind) == start + 2 == ledger_sum(db, e, kind)
    assert len(db.scalars(select(LeaveCarryConsumption).where(LeaveCarryConsumption.employee_id == e)).all()) == len(attributed)
    assert db.scalars(select(LeaveCarryExpiry).where(LeaveCarryExpiry.employee_id == e)).all() == []
