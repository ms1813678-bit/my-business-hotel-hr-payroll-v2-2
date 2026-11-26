"""Item 2h characterization: current cash-out behavior through the real review/apply services (real PostgreSQL).
Pins what the code does today (not a record of an owner decision): manual cash-out for any employee and any balance-tracked leave type, any time;
amount = (basic + performance bonus) / 30 per day, other bonus types excluded, money rounded once per operation;
payroll rows are never modified by the cash-out. New file only; reuses the existing fixtures. No new rule; not acceptance."""
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlalchemy import select

from app.leave_workflow import add_balance_entry, leave_balance
from app.models import LeaveBalanceTransaction, LeaveCashOut, LeaveType, PayrollItem
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_month_earning import monthly, post  # noqa: F401
from tests.test_leave_cash_out import auth_of, cash, counts, payroll


def item_state(item):
    return (item.salary_basis, item.base_salary, item.performance_bonus, item.overtime_amount,
            item.overtime_hours, item.updated_at)


def test_every_balance_tracked_type_cashes_exactly_and_payroll_rows_stay_untouched(monthly):
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    auth = auth_of(args)
    htype = db.get(LeaveType, kinds['H'])
    add_balance_entry(db, employee_id=e.id, leave_type=htype, transaction_type='adjustment', days_delta=Decimal(2),
                      reason='Synthetic holiday adjustment', actor='synthetic-workbook-store')
    _run, item = payroll(db, e, basic='3000.00', performance='600.00', status='closed')
    frozen = item_state(item)
    h_before = leave_balance(db, e.id, kinds['H'])
    # weekly 4/7 (released credit) and holiday 3/2 of 2: different types, same payroll item
    _r1, w = cash(db, auth, e, kinds['W'], item, '4/7', operation='1' * 64)
    _r2, h = cash(db, auth, e, kinds['H'], item, '3/2', operation='2' * 64)
    assert w['cash_egp'] == '68.57' and h['cash_egp'] == '180.00'     # 120 * 4/7, 120 * 3/2
    assert leave_balance(db, e.id, kinds['W']) == 0 and leave_balance(db, e.id, kinds['H']) == Fraction(1, 2)
    rows = db.scalars(select(LeaveCashOut).where(LeaveCashOut.employee_id == e.id).order_by(LeaveCashOut.id)).all()
    assert len(rows) == 2 and {r.payroll_item_id for r in rows} == {item.id} and len({r.ledger_id for r in rows}) == 2
    assert all(r.cash_egp > 0 for r in rows)
    # the cash-out never edits the payroll item or run; money is a recorded liability only
    assert item_state(item) == frozen and w['liability'] == h['liability'] == 'recorded_not_paid'


def test_other_bonus_is_excluded_and_future_date_is_refused_but_a_past_date_is_allowed(monthly):
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    auth = auth_of(args)
    _run, item = payroll(db, e, basic='3000.00', performance='600.00', status='approved')
    item.other_bonus = Decimal('5000.00')
    db.flush()
    from app.leave_cash_out import review_cash_out
    base = dict(employee_id=e.id, leave_type_id=kinds['W'], days='4/7', payroll_item_id=item.id, **auth)
    with pytest.raises(ValueError, match='current evaluation date'):
        review_cash_out(db, evaluated_on=date(2999, 1, 1), **base)
    _rv, result = cash(db, auth, e, kinds['W'], item, '4/7', evaluated=date(2026, 3, 5))
    assert result['cash_egp'] == '68.57'                               # other_bonus 5000 not in the basis
    row = db.scalar(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.id == result['ledger_id']))
    assert row.released_on == date(2026, 3, 5)


def test_item_cash_view_sums_each_receipt_once_is_stable_and_changes_nothing(monthly):
    from app.leave_cash_payroll_view import verified_cash_liabilities
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    auth = auth_of(args)
    _run, item = payroll(db, e, status='draft')
    _run2, other_item = payroll(db, e, status='draft', start=date(2026, 4, 1), end=date(2026, 4, 30))
    empty = verified_cash_liabilities(db, payroll_item_id=item.id)
    assert empty['receipts'] == [] and empty['total_cash_egp'] == '0.00'
    cash(db, auth, e, kinds['W'], item, '2/7', operation='3' * 64)
    cash(db, auth, e, kinds['W'], item, '2/7', operation='4' * 64)
    before = counts(db)
    view = verified_cash_liabilities(db, payroll_item_id=item.id)
    assert len(view['receipts']) == 2 and len({r['ledger_id'] for r in view['receipts']}) == 2
    # each receipt rounds once: 120 * 2/7 = 34.2857.. -> 34.29 each, total is the sum of the receipts (68.58)
    assert [r['cash_egp'] for r in view['receipts']] == ['34.29', '34.29'] and view['total_cash_egp'] == '68.58'
    assert view['posting_allowed'] is False and view['payroll_money_allowed'] is False
    assert verified_cash_liabilities(db, payroll_item_id=item.id) == view          # stable
    assert counts(db) == before
    assert verified_cash_liabilities(db, payroll_item_id=other_item.id)['receipts'] == []
    # a tampered receipt refuses the whole view instead of silently dropping or counting it
    row = db.scalars(select(LeaveCashOut).where(LeaveCashOut.payroll_item_id == item.id)).first()
    from sqlalchemy.exc import DBAPIError
    point = db.begin_nested()
    row.cash_egp = row.cash_egp + 1
    with pytest.raises(DBAPIError):
        db.flush()
    point.rollback()
    assert verified_cash_liabilities(db, payroll_item_id=item.id) == view
