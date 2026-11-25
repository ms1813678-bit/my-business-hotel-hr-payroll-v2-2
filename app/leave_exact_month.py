"""Exact rational monthly earning kernel, no persistence or release authority.

Finite manual decimal values and recurring accrual fractions stay exact. Only
an explicitly named display conversion may approximate; no Decimal context
or formatting precision participates in earning or balance arithmetic.
"""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction


@dataclass(frozen=True)
class ExactMonthEarned:
    annual: Fraction
    weekly: Fraction
    holiday: Fraction


def exact_decimal(value: Decimal) -> Fraction:
    if not isinstance(value,Decimal) or not value.is_finite():
        raise ValueError('Exact finite decimal required.')
    parts=value.as_tuple()
    if len(parts.digits)>4096 or abs(parts.exponent)>4096:
        raise ValueError('Exact arithmetic resource bound exceeded.')
    return Fraction(value)


def calculate_exact_month(*,tier,days_in_month,employed_days,unpaid_days,sick_days,
                          annual_deduct_unpaid,annual_deduct_sick,holiday_days):
    """Counts must come from disjoint complete authoritative month evidence."""
    if type(tier) is not int or tier not in (15,21,30):
        raise ValueError('Resolved annual tier required.')
    if type(days_in_month) is not int or days_in_month not in (28,29,30,31):
        raise ValueError('Explicit calendar month length required.')
    for n in (employed_days,unpaid_days,sick_days,holiday_days):
        if type(n) is not int or not 0<=n<=days_in_month:
            raise ValueError('Bounded whole-day evidence required.')
    if employed_days>days_in_month or unpaid_days+sick_days>employed_days or holiday_days>employed_days:
        raise ValueError('Month evidence coverage conflicts.')
    if type(annual_deduct_unpaid) is not bool or type(annual_deduct_sick) is not bool:
        raise ValueError('Explicit deduction policy required.')
    deductible=(unpaid_days if annual_deduct_unpaid else 0)+(sick_days if annual_deduct_sick else 0)
    return ExactMonthEarned(Fraction(tier*(employed_days-deductible),12*days_in_month),
        Fraction(employed_days-unpaid_days-sick_days,7),Fraction(holiday_days))
