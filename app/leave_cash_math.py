"""Exact leave cash calculation from explicit basic/performance inputs.

Pure calculation only. No balance debit, payroll creation, payment or approval.
The caller must resolve the actual employee/date/source and review authority.
"""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from app.leave_exact_amount import as_fraction


@dataclass(frozen=True)
class LeaveCashCalculation:
    days: Decimal | Fraction
    basic_salary: Decimal
    performance_bonus: Decimal
    daily_rate_exact: Fraction
    cash_exact: Fraction
    cash_egp: Decimal


def calculate_leave_cash(*, days, basic_salary, performance_bonus):
    """Basic plus performance, divided by30, round money once to cents.

    Other bonus types are not accepted as an argument. Cash days remain exact,
    including repeating ledger values. Half-up matches existing payroll money;
    no ambient Decimal precision/traps can round the day or rate truth.
    """
    exact_days = as_fraction(days)
    if exact_days <= 0:
        raise ValueError('Explicit positive exact cash days required.')
    for amount in (basic_salary, performance_bonus):
        if not isinstance(amount, Decimal):
            raise ValueError('Explicit finite nonnegative basic and performance amounts required.')
        if as_fraction(amount) < 0:
            raise ValueError('Explicit finite nonnegative basic and performance amounts required.')
    rate = (as_fraction(basic_salary) + as_fraction(performance_bonus)) / 30
    cash = exact_days * rate
    # Bound computed values too, before integer-to-text conversion.
    as_fraction(cash)
    scaled = cash * 100
    cents, remainder = divmod(scaled.numerator, scaled.denominator)
    if remainder * 2 >= scaled.denominator:
        cents += 1
    # String construction, not Decimal division/quantize under caller context.
    whole, fraction = divmod(cents, 100)
    money = Decimal(f'{whole}.{fraction:02d}')
    return LeaveCashCalculation(days, basic_salary, performance_bonus, rate, cash, money)


def cash_fits_numeric12_2(calculation):
    """Check actual payment column representation, not a business amount cap."""
    if not isinstance(calculation, LeaveCashCalculation):
        raise ValueError('Reviewed cash calculation required.')
    if calculation.cash_egp > Decimal('9999999999.99'):
        raise ValueError('Cash amount exceeds the payment column representation; nothing posted.')
    return calculation.cash_egp
