"""Characterization tests (source 778196c). Pins CURRENT behavior of pure carried-balance,
adjustment-preview and cash-out math, plus a custom-type payout pin. The four tests under the PostgreSQL
section need an alembic-head PG. Display ties-to-even assertions are display-only characterization.
This is not the carried-cycle acceptance (see test_l8_carried_balance_cycle_pg.py).
"""
from decimal import Decimal
from fractions import Fraction

import pytest

from app.leave_balance_rules import preview_manual_balance, validate_manual_balance_change
from app.leave_cash_math import calculate_leave_cash, cash_fits_numeric12_2
from app.leave_exact_amount import canonical_amount, display_days, exact_text, storage_amount


def test_carried_plus_adjustment_exact_sum_keeps_scale():
    assert preview_manual_balance(Decimal('7.5'), 'adjustment', Decimal('-0.25')) == Decimal('7.25')


def test_fraction_carry_plus_adjustment_returns_fraction_or_decimal_exactly():
    r = preview_manual_balance(Fraction(1, 3), 'adjustment', Decimal('1'))
    assert r == Fraction(4, 3) and isinstance(r, Fraction)


def test_zero_adjustment_and_carry_type_refused():
    with pytest.raises(ValueError):
        validate_manual_balance_change('adjustment', Decimal('0'))
    with pytest.raises(ValueError):
        validate_manual_balance_change('carry', Decimal('1'))


def test_negative_opening_refused_but_negative_adjustment_allowed():
    with pytest.raises(ValueError):
        validate_manual_balance_change('opening', Decimal('-1'))
    validate_manual_balance_change('adjustment', Decimal('-1'))


def test_payout_is_basic_plus_performance_over_30_rounded_half_up_once():
    c = calculate_leave_cash(days=Decimal('1'), basic_salary=Decimal('3000'), performance_bonus=Decimal('300'))
    assert c.daily_rate_exact == 110 and c.cash_egp == Decimal('110.00')
    c = calculate_leave_cash(days=Fraction(1, 3), basic_salary=Decimal('100'), performance_bonus=Decimal('0'))
    assert c.cash_exact == Fraction(10, 9) and c.cash_egp == Decimal('1.11')
    c = calculate_leave_cash(days=Decimal('0.15'), basic_salary=Decimal('100'), performance_bonus=Decimal('0'))
    assert c.cash_egp == Decimal('0.50')  # 0.5 exactly: half-up


@pytest.mark.parametrize('days', [Decimal('0'), Decimal('-1'), Fraction(0)])
def test_nonpositive_cash_days_refused(days):
    with pytest.raises(ValueError):
        calculate_leave_cash(days=days, basic_salary=Decimal('1'), performance_bonus=Decimal('0'))


def test_non_decimal_or_negative_amounts_refused():
    with pytest.raises(ValueError):
        calculate_leave_cash(days=Decimal('1'), basic_salary=3000, performance_bonus=Decimal('0'))
    with pytest.raises(ValueError):
        calculate_leave_cash(days=Decimal('1'), basic_salary=Decimal('1'), performance_bonus=Decimal('-1'))


def test_numeric12_2_limit_is_column_representation_only():
    ok = calculate_leave_cash(days=Decimal('30'), basic_salary=Decimal('9999999999.99'), performance_bonus=Decimal('0'))
    assert cash_fits_numeric12_2(ok) == Decimal('9999999999.99')
    big = calculate_leave_cash(days=Decimal('30'), basic_salary=Decimal('10000000000'), performance_bonus=Decimal('0'))
    with pytest.raises(ValueError):
        cash_fits_numeric12_2(big)


def test_storage_lane_decimal_vs_rational_and_display_ties_even():
    assert storage_amount(Fraction(3, 2))['days_delta'] == Decimal('1.5')
    s = storage_amount(Fraction(1, 3))
    assert s['days_delta'] is None and (s['exact_numerator'], s['exact_denominator']) == (1, 3)
    assert canonical_amount(Decimal('2.50')) == '5/2' and exact_text(Fraction(7, 3)) == '7/3'
    assert display_days(Decimal('0.125')) == '0.12' and display_days(Decimal('0.135')) == '0.14'


# ---- PostgreSQL-backed tests (need alembic-head PG; pattern from tests/test_leave_cash_out.py) ----
from datetime import date
from decimal import Decimal

import pytest

from app.leave_cash_out import review_cash_out
from app.leave_workflow import add_balance_entry, leave_balance
from app.models import Employee, LeaveType
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_cash_out import cash, payroll, counts


def _custom(db, employee, opening='5'):
    kind = LeaveType(code='synthetic_carry_2b', name_ar='x', name_en='Synthetic carry 2b',
                     tracks_balance=True, active=True)
    db.add(kind); db.flush()
    add_balance_entry(db, employee_id=employee.id, leave_type=kind, transaction_type='opening',
                      days_delta=Decimal(opening), reason='Synthetic carried opening', actor='synthetic-workbook-store')
    return kind


def test_carried_opening_then_adjustment_then_payout_uses_adjusted_balance(history_context):
    db, _a, auth, fact = history_context
    emp = db.get(Employee, fact.employee_id)
    kind = _custom(db, emp)
    add_balance_entry(db, employee_id=emp.id, leave_type=kind, transaction_type='adjustment',
                      days_delta=Decimal('-1.5'), reason='Synthetic adjustment', actor='synthetic-workbook-store')
    assert leave_balance(db, emp.id, kind.id) == Decimal('3.5')
    _run, item = payroll(db, emp, basic='3000.00', performance='300.00', status='approved')
    _r, result = cash(db, auth, emp, kind.id, item, Decimal('3.5'))
    assert result['cash_egp'] == '385.00'   # 3300/30 * 3.5
    assert leave_balance(db, emp.id, kind.id) == 0


def test_payout_above_adjusted_balance_refused_without_writes(history_context):
    db, _a, auth, fact = history_context
    emp = db.get(Employee, fact.employee_id)
    kind = _custom(db, emp)
    add_balance_entry(db, employee_id=emp.id, leave_type=kind, transaction_type='adjustment',
                      days_delta=Decimal('-1'), reason='Synthetic adjustment', actor='synthetic-workbook-store')
    _run, item = payroll(db, emp)
    before = counts(db)
    with pytest.raises(ValueError, match='exceed'):
        review_cash_out(db, employee_id=emp.id, leave_type_id=kind.id, days='4.5',
                        payroll_item_id=item.id, evaluated_on=date(2026, 4, 2), **auth)
    assert counts(db) == before


def test_negative_adjusted_balance_is_debt_never_cash(history_context):
    db, _a, auth, fact = history_context
    emp = db.get(Employee, fact.employee_id)
    kind = _custom(db, emp, opening='1')
    add_balance_entry(db, employee_id=emp.id, leave_type=kind, transaction_type='adjustment',
                      days_delta=Decimal('-2'), reason='Synthetic adjustment', actor='synthetic-workbook-store')
    _run, item = payroll(db, emp)
    with pytest.raises(ValueError, match='debt'):
        review_cash_out(db, employee_id=emp.id, leave_type_id=kind.id, days='1',
                        payroll_item_id=item.id, evaluated_on=date(2026, 4, 2), **auth)


def test_payout_basis_is_salary_basis_not_prorated_base_salary(history_context):
    db, _a, auth, fact = history_context
    emp = db.get(Employee, fact.employee_id)
    kind = _custom(db, emp, opening='3')
    _run, item = payroll(db, emp, basic='3000.00', performance='0.00')
    item.base_salary = Decimal('1500.00'); db.flush()   # prorated payable differs from basis
    _r, result = cash(db, auth, emp, kind.id, item, Decimal('3'))
    assert result['cash_egp'] == '300.00'  # 3000/30*3, base_salary ignored
