from decimal import Decimal, Inexact, Rounded, localcontext
from fractions import Fraction

import pytest

from app.leave_cash_math import calculate_leave_cash


def cash(days, basic=Decimal('3000'), performance=Decimal('300')):
    return calculate_leave_cash(days=days, basic_salary=basic, performance_bonus=performance)


def test_cash_basis_excludes_other_bonus_and_always_divides_by_thirty():
    result = cash(Fraction(1, 7))
    assert result.daily_rate_exact == 110
    assert result.cash_exact == Fraction(110, 7)
    assert result.cash_egp == Decimal('15.71')
    with pytest.raises(TypeError):
        calculate_leave_cash(days=Decimal(1), basic_salary=Decimal(3000),
                             performance_bonus=Decimal(300), other_bonus=Decimal(900))


def test_tiny_context_and_traps_do_not_round_days_or_cash_intermediates():
    expected = cash(Fraction(13, 21), Decimal('1234.56789'), Decimal('78.901234'))
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        assert cash(Fraction(13, 21), Decimal('1234.56789'), Decimal('78.901234')) == expected
    assert expected.cash_exact == Fraction(13, 21) * Fraction(Decimal('1313.469124')) / 30


def test_money_rounds_once_not_daily_before_multiplication():
    # Exact daily rate 1/3: 3 days =1, not0.99 from a rounded rate.
    result = cash(Decimal(3), Decimal(10), Decimal(0))
    assert result.daily_rate_exact == Fraction(1, 3)
    assert result.cash_exact == 1 and result.cash_egp == Decimal('1.00')
    assert cash(Decimal('0.015'), Decimal(30), Decimal(0)).cash_egp == Decimal('0.02')


@pytest.mark.parametrize('change', [dict(days=Decimal(0)), dict(days=Fraction(-1, 7)),
    dict(days=True), dict(days=1.0), dict(basic_salary=Decimal('-1')),
    dict(performance_bonus=Decimal('NaN')), dict(basic_salary=3000),
    dict(days=Fraction(10 ** 5000, 3))])
def test_invalid_or_oversized_explicit_inputs_refuse(change):
    values = dict(days=Fraction(1, 7), basic_salary=Decimal(3000), performance_bonus=Decimal(300))
    with pytest.raises(ValueError):
        calculate_leave_cash(**(values | change))


def test_zero_pay_basis_is_zero_not_missing_evidence():
    result = cash(Fraction(2, 7), Decimal(0), Decimal(0))
    assert result.cash_exact == 0 and result.cash_egp == Decimal('0.00')


def test_payment_storage_envelope_is_explicit_separate_from_pure_math():
    from app.leave_cash_math import cash_fits_numeric12_2
    assert cash_fits_numeric12_2(cash(Decimal(1), Decimal('299999999999.70'), Decimal(0))) == Decimal('9999999999.99')
    oversized = cash(Decimal(1), Decimal('300000000000'), Decimal(0))
    assert oversized.cash_egp == Decimal('10000000000.00')
    with pytest.raises(ValueError, match='column representation'):
        cash_fits_numeric12_2(oversized)
