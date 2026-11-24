from decimal import Decimal, localcontext

import pytest

from app.leave_day_calculations import (
    annual_month_accrued,
    day_balance,
    resolved_code,
    weekly_accrued,
    year_end_days,
)


def test_original_weekly_acceptance():
    value = weekly_accrued(334, 7, 7)
    assert str(value).startswith('45.714285714285714285714285')
    with localcontext() as context:
        context.prec = 50
        assert value == Decimal(320) / Decimal(7)


def test_original_june_year_end_acceptance():
    accrued = sum(annual_month_accrued(15, days, days, 0)
                  for days in [30, 31, 31, 30, 31, 30, 31])
    assert accrued == Decimal('8.75')
    split = year_end_days(accrued, accrued)
    assert split.cash_days == Decimal('4.375')
    assert split.excess == Decimal('4.375')


@pytest.mark.parametrize(('tier', 'remaining', 'cash', 'excess'), [
    (15, '12', '7.5', '4.5'), (21, '8', '8', '0'), (30, '30', '15', '15')])
def test_original_year_end_tiers(tier, remaining, cash, excess):
    result = year_end_days(Decimal(remaining), Decimal(tier))
    assert result.cash_days == Decimal(cash)
    assert result.excess == Decimal(excess)


def test_negative_is_carried_not_cash():
    result = year_end_days(Decimal('-3.125'), Decimal(15))
    assert result.negative_carry == Decimal('-3.125')
    assert result.cash_days == result.excess == 0
    assert day_balance(Decimal(-2), Decimal('1.25'), Decimal(3)) == Decimal('-3.75')


def test_first_month_and_end_work_use_only_employed_days():
    assert annual_month_accrued(15, 30, 15, 0) == Decimal('0.625')
    assert annual_month_accrued(15, 30, 0, 0) == 0
    assert annual_month_accrued(30, 30, 15, 3) == 1


@pytest.mark.parametrize(('old', 'new', 'result'), [
    ('w', 's', 'W'), ('H', 'S', 'H'), ('A', 's', 'A'), ('S', 'u', 'S'),
    (None, 's', 'S'), ('u', 'U', 'U')])
def test_precedence_preserves_existing_leave(old, new, result):
    assert resolved_code(old, new) == result


def test_overwrite_requires_explicit_workflow_decision():
    with pytest.raises(ValueError, match='HR'):
        resolved_code('A', 'W')
    assert resolved_code('A', 'W', hr_override=True) == 'W'
    with pytest.raises(ValueError, match='Unknown'):
        resolved_code(None, '')


@pytest.mark.parametrize('value', [1.5, Decimal('1.5'), Decimal('NaN'), Decimal('Infinity'), True, -1])
def test_invalid_counts_never_silently_coerced(value):
    with pytest.raises((TypeError, ValueError)):
        weekly_accrued(value, 0, 0)


def test_counts_cannot_exceed_employment():
    with pytest.raises(ValueError):
        weekly_accrued(10, 7, 7)
    with pytest.raises(ValueError):
        annual_month_accrued(15, 30, 31, 0)
    with pytest.raises(ValueError):
        annual_month_accrued(15, 30, 10, 11)


def test_employed_dates_stop_at_authoritative_end_and_keep_gaps():
    from datetime import date

    from app.leave_day_calculations import employed_dates
    days = employed_dates(date(2026, 1, 1), date(2026, 1, 31), [
        (date(2025, 12, 1), date(2026, 1, 4)),
        (date(2026, 1, 10), date(2026, 1, 12)),
        (date(2026, 1, 11), date(2026, 1, 12)),
    ])
    assert len(days) == 7
    assert date(2026, 1, 12) in days
    assert date(2026, 1, 13) not in days
    assert date(2026, 1, 5) not in days
    assert weekly_accrued(len(days), 0, 0) == 1


def test_future_hire_and_open_service_period_are_clipped():
    from datetime import date

    from app.leave_day_calculations import employed_dates
    assert not employed_dates(date(2026, 1, 1), date(2026, 1, 31), [
        (date(2026, 2, 1), None)])
    assert len(employed_dates(date(2026, 1, 1), date(2026, 1, 31), [
        (date(2026, 1, 20), None)])) == 12


def test_holiday_credit_uses_employment_and_explicit_sick_setting():
    from datetime import date

    from app.leave_day_calculations import holiday_credit
    employed = frozenset({date(2026, 1, 1), date(2026, 1, 2)})
    holidays = {date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)}
    sick = {date(2026, 1, 2)}
    assert holiday_credit(holidays, employed, sick, credit_during_sick=False) == 1
    assert holiday_credit(holidays, employed, sick, credit_during_sick=True) == 2
    with pytest.raises(TypeError):
        holiday_credit(holidays, employed, sick, credit_during_sick=None)


def test_sick_counter_requires_supplied_period_and_never_caps():
    from datetime import date, timedelta

    from app.leave_day_calculations import sick_days_in_period
    first = date(2026, 1, 1)
    employed = frozenset(first + timedelta(days=i) for i in range(200))
    assert sick_days_in_period(set(employed), employed, first, first + timedelta(days=199)) == 200
    assert sick_days_in_period(set(employed), employed, first, first + timedelta(days=89)) == 90


def test_sick_alert_thresholds_warn_without_rejecting_or_capping():
    from datetime import date, timedelta

    from app.leave_day_calculations import sick_alert_state
    first = date(2026, 1, 1)
    employed = frozenset(first + timedelta(days=i) for i in range(200))
    state = sick_alert_state(set(employed), employed, first, first + timedelta(days=199),
        thresholds=(90, 180), certificate_flags={first: True, first + timedelta(days=1): False})
    assert state.days == 200
    assert state.reached_thresholds == (90, 180)
    assert state.missing_certificate_days == 199
    assert employed == frozenset(first + timedelta(days=i) for i in range(200))


def test_sick_alerts_require_explicit_valid_policy_inputs():
    from datetime import date

    from app.leave_day_calculations import sick_alert_state
    day = date(2026, 1, 1)
    with pytest.raises(ValueError):
        sick_alert_state({day}, frozenset({day}), day, day, thresholds=(90, 90), certificate_flags={})
    with pytest.raises(TypeError):
        sick_alert_state({day}, frozenset({day}), day, day, thresholds=(90, 180), certificate_flags={day: 'yes'})


@pytest.mark.parametrize('tier', [15.0, Decimal(15), True, '15'])
def test_annual_tier_requires_explicit_integer_not_numeric_coercion(tier):
    with pytest.raises(ValueError, match='Annual tier'):
        annual_month_accrued(tier, 31, 31, 0)
