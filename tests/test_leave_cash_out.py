"""Explicit manual leave cash-out kernel tests. Synthetic data only.

Real migrated PostgreSQL (conftest requires alembic head; after this patch the
head is 0074_leave_cash_out). Reuses the existing `monthly` fixture (verified
manual opening + verified month earning) and `history_context`. No auth is
monkeypatched: the service re-checks the real _guard identity on every call.
"""
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlalchemy import func, select, text

from app.leave_cash_out import apply_cash_out, review_cash_out
from app.leave_workflow import add_balance_entry, leave_balance
from app.models import (AuditLog, LeaveBalanceTransaction, LeaveCashOut, LeaveType,
                        PayrollItem, PayrollRun)
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_month_earning import monthly, post  # noqa: F401

REASON = 'Synthetic reviewed cash-out'


def payroll(db, employee, *, basic='3000.00', performance='600.00', status='approved',
            start=date(2026, 3, 1), end=date(2026, 3, 31)):
    run = PayrollRun(period_start=start, period_end=end, pay_date=end, status=status,
                     created_by='synthetic')
    db.add(run); db.flush()
    item = PayrollItem(payroll_run_id=run.id, employee_id=employee.id,
        employee_code_snapshot=employee.code, employee_name_snapshot=employee.full_name,
        department_name_snapshot='Synthetic', position_snapshot=employee.position,
        salary_basis=Decimal(basic), base_salary=Decimal(basic), daily_hours=Decimal(8),
        performance_bonus=Decimal(performance))
    db.add(item); db.flush()
    return run, item


def auth_of(args):
    # The monthly fixture's selection dict already carries the real auth identity.
    selection = args(date(2026, 2, 1), date(2026, 2, 28))
    return {k: selection[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}


def cash(db, auth, employee, type_id, item, days, operation='d' * 64, evaluated=date(2026, 4, 2)):
    args = dict(employee_id=employee.id, leave_type_id=type_id, days=days,
                payroll_item_id=item.id, evaluated_on=evaluated, **auth)
    review = review_cash_out(db, **args)
    result = apply_cash_out(db, operation_id=operation, reviewed_digest=review['reviewed_digest'],
                            human_confirmed=True, reason=REASON, **args)
    return review, result


def counts(db):
    return {m.__tablename__: db.scalar(select(func.count()).select_from(m))
            for m in (LeaveCashOut, LeaveBalanceTransaction, AuditLog)}


def test_verified_opening_monthly_fraction_cashout_and_exact_retry(monthly):
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    post(db, args(date(2026, 3, 1), date(2026, 4, 1)), 'b' * 64)
    assert leave_balance(db, e.id, kinds['A']) == Fraction(3, 7)  # released source only
    _run, item = payroll(db, e)
    before = counts(db)
    review, result = cash(db, auth_of(args), e, kinds['A'], item, '3/7')
    assert result['cash_egp'] == '51.43'  # (3000+600)/30 * 3/7 = 360/7, HALF_UP once
    assert leave_balance(db, e.id, kinds['A']) == 0
    after = counts(db)
    assert after['leave_cash_outs'] == before['leave_cash_outs'] + 1
    assert after['leave_balance_transactions'] == before['leave_balance_transactions'] + 1
    # Exact retry: same operation + reviewed digest straight to apply (the
    # route confirm path never re-reviews), same verified result, no 2nd debit.
    again = apply_cash_out(db, operation_id='d' * 64, reviewed_digest=review['reviewed_digest'],
        human_confirmed=True, reason=REASON, employee_id=e.id, leave_type_id=kinds['A'],
        days=Fraction(3, 7), payroll_item_id=item.id, evaluated_on=date(2026, 4, 2), **auth_of(args))
    assert again == result
    assert counts(db) == after
    # Different operation cannot spend what is no longer there.
    with pytest.raises(ValueError):
        cash(db, auth_of(args), e, kinds['A'], item, '1', operation='e' * 64)



def test_held_annual_and_debt_are_never_cashed(monthly):
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))  # annual held; A balance = -1 (usage)
    _run, item = payroll(db, e)
    auth = auth_of(args)
    with pytest.raises(ValueError, match='debt'):
        review_cash_out(db, employee_id=e.id, leave_type_id=kinds['A'], days='1',
                        payroll_item_id=item.id, evaluated_on=date(2026, 4, 2), **auth)
    # Weekly balance 4/7 is released ledger credit and cashes exactly.
    _review, result = cash(db, auth, e, kinds['W'], item, '4/7')
    assert result['cash_egp'] == '68.57'  # 120 * 4/7 = 480/7
    assert leave_balance(db, e.id, kinds['W']) == 0


def test_custom_tracked_type_via_audit_chain_opening(history_context):
    db, _account, auth, fact = history_context
    from app.models import Employee
    employee = db.get(Employee, fact.employee_id)
    custom = LeaveType(code='synthetic_custom', name_ar='مخصص', name_en='Synthetic custom',
                       tracks_balance=True, active=True)
    db.add(custom); db.flush()
    add_balance_entry(db, employee_id=employee.id, leave_type=custom, transaction_type='opening',
                      days_delta=Decimal(5), reason='Synthetic custom opening', actor='synthetic-workbook-store')
    _run, item = payroll(db, employee, basic='1500.00', performance='0.00', status='draft')
    _review, result = cash(db, auth, employee, custom.id, item, Decimal(2))
    assert result['cash_egp'] == '100.00'  # (1500+0)/30 * 2
    assert leave_balance(db, employee.id, custom.id) == Decimal(3)
    # A non-tracking type is refused: counters do not become a balance.
    counter = LeaveType(code='synthetic_counter', name_ar='عداد', name_en='Counter',
                        tracks_balance=False, active=True)
    db.add(counter); db.flush()
    with pytest.raises(ValueError, match='balance-tracked'):
        review_cash_out(db, employee_id=employee.id, leave_type_id=counter.id, days='1',
                        payroll_item_id=item.id, evaluated_on=date(2026, 4, 2), **auth)


def test_stale_basis_and_deactivation_refuse_without_writes(monthly):
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    _run, item = payroll(db, e)
    auth = auth_of(args)
    review_args = dict(employee_id=e.id, leave_type_id=kinds['W'], days='2/7',
                       payroll_item_id=item.id, evaluated_on=date(2026, 4, 2), **auth)
    review = review_cash_out(db, **review_args)
    before = counts(db)
    item.performance_bonus = Decimal('601.00'); db.flush()  # basis changed after review
    with pytest.raises(ValueError, match='review again'):
        apply_cash_out(db, operation_id='f' * 64, reviewed_digest=review['reviewed_digest'],
                       human_confirmed=True, reason=REASON, **review_args)
    assert counts(db) == before
    item.performance_bonus = Decimal('600.00'); db.flush()
    e.status = 'inactive'; db.flush()
    with pytest.raises(ValueError, match='Active employee'):
        review_cash_out(db, **review_args)


def test_overflow_refused_before_any_write(history_context):
    db, _account, auth, fact = history_context
    from app.models import Employee
    employee = db.get(Employee, fact.employee_id)
    custom = LeaveType(code='synthetic_big', name_ar='كبير', name_en='Synthetic big',
                       tracks_balance=True, active=True)
    db.add(custom); db.flush()
    add_balance_entry(db, employee_id=employee.id, leave_type=custom, transaction_type='opening',
                      days_delta=Decimal(90000000), reason='Synthetic big opening', actor='synthetic-workbook-store')
    _run, item = payroll(db, employee, basic='9999999999.99', performance='0.00')
    before = counts(db)
    with pytest.raises(ValueError, match='representation'):
        review_cash_out(db, employee_id=employee.id, leave_type_id=custom.id, days=Decimal(90000000),
                        payroll_item_id=item.id, evaluated_on=date(2026, 4, 2), **auth)
    assert counts(db) == before


def test_receipt_immutable_and_populated_downgrade_refuses(monthly):
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    _run, item = payroll(db, e)
    cash(db, auth_of(args), e, kinds['W'], item, '2/7')
    db.flush()
    for statement in ("UPDATE leave_cash_outs SET cash_egp = 1",
                      "DELETE FROM leave_cash_outs",
                      "TRUNCATE leave_cash_outs CASCADE"):
        with pytest.raises(Exception, match='immutable'):
            with db.begin_nested():
                db.execute(text(statement)); db.flush()
    # Populated downgrade refusal is exercised against a scratch database in
    # tests/test_leave_cash_out_migration_pg.py (alembic downgrade -1).
