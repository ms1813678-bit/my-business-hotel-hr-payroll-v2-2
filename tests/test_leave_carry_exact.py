"""Exact grant descriptors preserve ledger truth; no operational posting grant."""
from datetime import date
from decimal import Decimal, Inexact, Rounded, localcontext
from fractions import Fraction

import pytest

from app.leave_carry_grant import CarryGrant, grant_window_contains


def grant(days):
    return CarryGrant(1, 'A', days, date(2026, 12, 31), date(2027, 1, 3),
                      date(2027, 6, 17), 'Synthetic explicit carry window')


def test_repeating_grant_keeps_original_exact_value_under_tiny_decimal_context():
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        row = grant(Fraction(1, 7))
    assert row.days == Fraction(1, 7) and type(row.days) is Fraction
    assert grant_window_contains(row, date(2027, 1, 3))
    assert grant_window_contains(row, date(2027, 6, 17))
    assert not grant_window_contains(row, date(2027, 6, 18))
    assert row.days == Fraction(1, 7)  # availability query never erases value


def test_legacy_decimal_keeps_value_and_scale():
    amount = Decimal('1.234567890123456789123456780')
    row = grant(amount)
    assert row.days is amount and row.days.as_tuple() == amount.as_tuple()


@pytest.mark.parametrize('amount', [Fraction(0), Fraction(-1, 7), True, 1, 0.1,
                                     Decimal('Infinity'), Decimal('NaN'),
                                     Fraction(1, 10 ** 5000)])
def test_exact_grant_remains_positive_bounded_not_debt_or_float(amount):
    with pytest.raises(ValueError):
        grant(amount)


def test_non_january_grant_and_unusual_chosen_window_have_no_implicit_expiry_action():
    row = CarryGrant(1, 'W', Fraction(9, 7), date(2026, 4, 2), date(2026, 4, 4),
                     date(2028, 7, 9), 'Synthetic explicit long window')
    assert grant_window_contains(row, date(2028, 7, 9))
    assert not grant_window_contains(row, date(2028, 7, 10))
    assert row.days == Fraction(9, 7)
