"""Property‑based tests for the exact leave‑cash calculation.

These tests exercise the *actual* public contract of
`app.leave_cash_math.calculate_leave_cash` and the helper
`cash_fits_numeric12_2` without relying on the implementation’s
intermediate Decimal arithmetic.  An independent oracle works only
with integers / `Fraction` objects, performs the half‑up cent rounding
explicitly, and is immune to the caller’s Decimal context.
"""

from __future__ import annotations

from decimal import Decimal, Inexact, Rounded, localcontext
from fractions import Fraction

import pytest

from app.leave_cash_math import (
    LeaveCashCalculation,
    calculate_leave_cash,
    cash_fits_numeric12_2,
)
from app.leave_exact_amount import as_fraction


# ----------------------------------------------------------------------
# Helper – independent oracle that mirrors the contract of `calculate_leave_cash`
# ----------------------------------------------------------------------
def _oracle_cash(days, basic: Decimal, performance: Decimal) -> tuple[Fraction, Decimal]:
    """
    Return ``(cash_exact, cash_egp)`` computed *exactly* with Fractions.

    * ``days`` – ``Decimal`` or ``Fraction`` (will be converted with ``as_fraction``).
    * ``basic`` / ``performance`` – must be ``Decimal`` (the contract requires this).

    The algorithm follows the implementation:
        rate = (basic + performance) / 30               (exact Fraction)
        cash = days * rate                              (exact Fraction)
        cents = round_half_up(cash * 100)               (integer cents)
        cash_egp = Decimal(cents / 100)                 (two‑decimal string)
    """
    exact_days = as_fraction(days)
    # rate as exact Fraction – Decimal → Fraction conversion is exact
    rate = (as_fraction(basic) + as_fraction(performance)) / 30
    cash = exact_days * rate

    # ----- half‑up rounding to the nearest cent -----
    scaled = cash * 100                     # still a Fraction
    cents, remainder = divmod(scaled.numerator, scaled.denominator)
    if remainder * 2 >= scaled.denominator:   # half‑up tie goes up
        cents += 1

    # build the canonical two‑decimal Decimal **without** any division in the current context
    whole, fraction = divmod(cents, 100)
    cash_egp = Decimal(f"{whole}.{fraction:02d}")

    return cash, cash_egp


# ----------------------------------------------------------------------
# Basic sanity checks – the oracle must match the implementation
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "days,basic,perf",
    [
        (Fraction(1, 7), Decimal("3000"), Decimal("300")),
        (Fraction(13, 21), Decimal("1234.56789"), Decimal("78.901234")),
        (Decimal(3), Decimal(10), Decimal(0)),
        (Decimal("0.015"), Decimal(30), Decimal(0)),
        (Fraction(2, 7), Decimal(0), Decimal(0)),
    ],
)
def test_calculate_leave_cash_matches_oracle(days, basic, perf):
    """Exact cash and rounding must agree with an independent Fraction oracle."""
    calc = calculate_leave_cash(days=days, basic_salary=basic, performance_bonus=perf)
    expected_exact, expected_egp = _oracle_cash(days, basic, perf)

    assert isinstance(calc, LeaveCashCalculation)
    # exact Fraction value
    assert calc.cash_exact == expected_exact
    # two‑decimal Decimal after half‑up rounding
    assert calc.cash_egp == expected_egp


# ----------------------------------------------------------------------
# Half‑cent (0.005 EGP) rounding boundary – tie must round **up**
# ----------------------------------------------------------------------
def test_half_cent_rounds_up():
    """
    Construct a scenario where the exact cash is X + 0.005.
    With half‑up policy the result must be X + 0.01.
    """
    # cash_exact = 1.005 = 201/200
    # Choose 1 day, rate = 1.005 → basic = 30 * 1.005 = 30.15
    days = Decimal(1)
    basic = Decimal("30.15")
    perf = Decimal(0)

    calc = calculate_leave_cash(days=days, basic_salary=basic, performance_bonus=perf)

    # Verify the exact cash matches the crafted fraction
    assert calc.cash_exact == Fraction(201, 200)

    # Rounded amount must be 1.01 (not 1.00)
    assert calc.cash_egp == Decimal("1.01")


# ----------------------------------------------------------------------
# Decimal context independence – low precision / traps must not affect the result
# ----------------------------------------------------------------------
def test_decimal_context_does_not_affect_calculation():
    """Even with a tiny Decimal context the function must produce the same result."""
    days = Fraction(13, 21)
    basic = Decimal("1234.56789")
    perf = Decimal("78.901234")

    # Expected result obtained once with the default context
    expected = calculate_leave_cash(days=days, basic_salary=basic, performance_bonus=perf)

    # Run the call under a deliberately restrictive context
    with localcontext() as ctx:
        ctx.prec = 2                 # absurdly low precision
        ctx.traps[Inexact] = True    # raise on any inexact operation
        ctx.traps[Rounded] = True    # raise on any rounding
        # The implementation should *not* hit any of those traps.
        result = calculate_leave_cash(days=days, basic_salary=basic, performance_bonus=perf)

    assert result == expected
    # also compare with the independent oracle to ensure we are still correct
    expected_exact, expected_egp = _oracle_cash(days, basic, perf)
    assert result.cash_exact == expected_exact
    assert result.cash_egp == expected_egp


# ----------------------------------------------------------------------
# cash_fits_numeric12_2 – boundary and type checks
# ----------------------------------------------------------------------
def test_cash_fits_numeric12_2_accepts_maximum():
    """
    The payment column is a NUMERIC(12,2) → maximum representable value is
    9_999_999_999.99.  Build a calculation that lands exactly on that value
    and verify the helper returns the Decimal unchanged.
    """
    # We need cash_egp == 9_999_999_999.99.
    # Choose 1 day and a rate that yields that amount:
    # cash = days * rate → rate = cash / days.
    # rate = 9_999_999_999.99 → (basic+perf) = rate * 30
    rate = Decimal("9999999999.99")
    total = rate * 30
    # Split total arbitrarily between basic and performance.
    basic = (total // 2).quantize(Decimal("1"))
    perf = total - basic
    calc = calculate_leave_cash(days=Decimal(1), basic_salary=basic, performance_bonus=perf)
    # sanity check that we really hit the limit
    assert calc.cash_egp == Decimal("9999999999.99")
    assert cash_fits_numeric12_2(calc) == Decimal("9999999999.99")


def test_cash_fits_numeric12_2_rejects_oversize():
    """Any amount > 9_999_999_999.99 must raise ValueError."""
    # Use a slightly larger amount than the previous test.
    rate = Decimal("10000000000.00")  # exactly 10 billion
    total = rate * 30
    basic = (total // 2).quantize(Decimal("1"))
    perf = total - basic
    calc = calculate_leave_cash(days=Decimal(1), basic_salary=basic, performance_bonus=perf)
    assert calc.cash_egp == Decimal("10000000000.00")
    with pytest.raises(ValueError, match="payment column representation"):
        cash_fits_numeric12_2(calc)


def test_cash_fits_numeric12_2_invalid_argument_type():
    """Passing anything other than a LeaveCashCalculation must raise."""
    with pytest.raises(ValueError, match="Reviewed cash calculation required"):
        cash_fits_numeric12_2("not a calculation")  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# API contract – invalid or non‑finite inputs are rejected
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "days,basic,perf,expected_msg",
    [
        (Decimal(0), Decimal("3000"), Decimal("300"), "Explicit positive exact cash days required."),
        (Fraction(-1, 7), Decimal("3000"), Decimal("300"), "Explicit positive exact cash days required."),
        (Fraction(1, 2), Decimal("-1"), Decimal("0"), "Explicit finite nonnegative basic and performance amounts required."),
        (Fraction(1, 2), Decimal("0"), Decimal("NaN"), "Ledger contains nonfinite day evidence."),
        (Fraction(1, 2), 3000, Decimal("0"), "Explicit finite nonnegative basic and performance amounts required."),
        (Fraction(10 ** 5000, 3), Decimal("3000"), Decimal("300"), "Exact arithmetic resource bound exceeded."),
    ],
)
def test_invalid_inputs_raise(days, basic, perf, expected_msg):
    """Each violation of the public contract raises a ValueError with the correct message."""
    with pytest.raises(ValueError) as exc:
        calculate_leave_cash(days=days, basic_salary=basic, performance_bonus=perf)
    assert expected_msg in str(exc.value)
