"""Edge and metamorphic tests for app.payroll_math (Task A)."""
import math
from datetime import date
from decimal import Decimal

import pytest

from app.payroll_math import (
    absence_deduction,
    hourly_rate,
    lateness_deduction,
    money,
    overtime_hourly_rate,
    prorated_new_hire_salary,
)

D = Decimal

def test_money_half_up_at_exact_half_cent_rounds_away_from_zero():
    assert money(D("1.005")) == D("1.01")
    assert money(D("-1.005")) == D("-1.01")

def test_money_below_half_cent_stays_put_in_both_directions():
    assert money(D("1.004")) == D("1.00")
    assert money(D("-1.004")) == D("-1.00")

def test_hourly_rate_rejects_negative_salary_and_nonpositive_hours():
    with pytest.raises(ValueError):
        hourly_rate(D("-1"), D("8"))
    with pytest.raises(ValueError):
        hourly_rate(D("9000"), D("0"))
    with pytest.raises(ValueError):
        hourly_rate(D("9000"), D("-8"))

@pytest.mark.parametrize("bad", [9000, float("nan"), float("inf")])
def test_ot_rate_rejects_non_decimal_or_non_finite_salary(bad):
    with pytest.raises(ValueError):
        overtime_hourly_rate(bad, D("0"), D("8"))

def test_ot_rate_rejects_non_finite_bonus_and_hours():
    with pytest.raises(ValueError):
        overtime_hourly_rate(D("9000"), float("nan"), D("8"))
    with pytest.raises(ValueError):
        overtime_hourly_rate(D("9000"), D("0"), math.inf)

def test_ot_rate_rejects_negative_bonus_and_zero_hours():
    with pytest.raises(ValueError):
        overtime_hourly_rate(D("9000"), D("-1"), D("8"))
    with pytest.raises(ValueError):
        overtime_hourly_rate(D("9000"), D("0"), D("0"))

def test_lateness_at_exact_grace_boundary_yields_zero():
    # 10 late == late grace 10; 5 early == early grace 5 -> nothing chargeable.
    assert lateness_deduction(D("9000"), D("8"), late_minutes=10, early_minutes=5) == D("0.00")

def test_lateness_one_minute_past_both_graces_is_charged():
    # 11 late -> 1 chargeable; 6 early -> 1 chargeable; 2/60 h * 37.5 = 1.25
    assert lateness_deduction(D("9000"), D("8"), 11, 6) == D("1.25")

def test_lateness_custom_graces_override_defaults():
    # late grace 20 -> 5 chargeable; early grace 0 -> 5 chargeable
    # 10/60 h * 37.5 = 6.25
    result = lateness_deduction(D("9000"), D("8"), 25, 5,
                                late_grace_minutes=20, early_grace_minutes=0)
    assert result == D("6.25")

@pytest.mark.parametrize("kwargs", [
    {"late_minutes": -1}, {"early_minutes": -1},
    {"late_grace_minutes": -1}, {"early_grace_minutes": -1},
])
def test_lateness_rejects_negative_minutes(kwargs):
    with pytest.raises(ValueError):
        lateness_deduction(D("9000"), D("8"), **kwargs)

def test_absence_leap_february_full_month_equals_salary():
    assert absence_deduction(D("2900"), 2024, 2, 29) == D("2900.00")

def test_absence_non_leap_february_one_day():
    # 2900 / 28 = 103.571... -> 103.57
    assert absence_deduction(D("2900"), 2025, 2, 1) == D("103.57")

def test_absence_days_equal_month_length_equals_full_salary():
    assert absence_deduction(D("9000"), 2026, 1, 31) == D("9000.00")

@pytest.mark.parametrize("days", [31, -1])
def test_absence_rejects_days_beyond_or_below_month_length(days):
    with pytest.raises(ValueError):
        absence_deduction(D("9000"), 2026, 4, days)  # April has 30 days

def test_new_hire_on_day_one_equals_full_salary():
    assert prorated_new_hire_salary(D("9000"), date(2026, 1, 1)) == D("9000.00")

def test_new_hire_on_last_day_equals_one_day():
    # 9000 / 31 = 290.322... -> 290.32
    assert prorated_new_hire_salary(D("9000"), date(2026, 1, 31)) == D("290.32")

def test_doubling_salary_doubles_outputs_within_one_cent():
    functions = [
        lambda s: hourly_rate(s, D("8")),
        lambda s: lateness_deduction(s, D("8"), 20, 10),
        lambda s: absence_deduction(s, 2026, 2, 3),
        lambda s: prorated_new_hire_salary(s, date(2026, 2, 10)),
    ]
    for fn in functions:
        assert abs(fn(D("9000")) * 2 - fn(D("18000"))) <= D("0.01")

def test_overtime_rate_is_linear_in_salary_and_bonus():
    base = overtime_hourly_rate(D("6000"), D("1200"), D("8"))
    bumped = overtime_hourly_rate(D("6000") + D("800"), D("1200") + D("400"), D("8"))
    assert base + (D("800") + D("400")) / (D(30) * D(8)) == bumped
