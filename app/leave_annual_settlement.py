"""Exact approved annual cash-day split, no payroll/ledger posting.

Full annual entitlement basis is explicit. Caller resolves actual tier/year and
remaining source. Current confirmed policy: half full entitlement, capped by
positive remaining, with November/December new-hire annual preservation.
"""
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from app.leave_exact_amount import as_fraction


@dataclass(frozen=True)
class AnnualSettlementSplit:
    cash_days: Fraction
    carried_days: Fraction
    cleared_days: Fraction
    held_days: Fraction
    annual_full_entitlement: int
    late_year_preservation: bool


def annual_settlement_split(*, full_annual_entitlement, remaining, hire_date,
                            source_year, other_half_action,
                            annual_release_proven):
    if (type(full_annual_entitlement) is not int or full_annual_entitlement not in (15, 21, 30)
            or type(source_year) is not int or not 1 <= source_year <= 9998
            or type(hire_date) is not date or hire_date > date(source_year, 12, 31)
            or type(annual_release_proven) is not bool):
        raise ValueError('Explicit supported full entitlement, source year and hire/release evidence required.')
    amount = as_fraction(remaining)
    late = hire_date.year == source_year and hire_date.month in (11, 12)
    if amount <= 0:
        if other_half_action != 'carry':
            raise ValueError('Zero or debt remainder requires explicit carry, no cash or erasure.')
        return AnnualSettlementSplit(Fraction(), amount, Fraction(), Fraction(),
                                     full_annual_entitlement, late)
    if late:
        if other_half_action != 'carry':
            raise ValueError('November/December annual remainder is preserved, not cashed or cleared.')
        return AnnualSettlementSplit(Fraction(), amount, Fraction(),
                                     Fraction() if annual_release_proven else amount,
                                     full_annual_entitlement, True)
    if not annual_release_proven:
        raise ValueError('Unreleased annual remainder is not cash eligible.')
    if other_half_action not in ('carry', 'zero'):
        raise ValueError('Explicit HR carry or zero selection required.')
    cash = min(amount, Fraction(full_annual_entitlement, 2))
    excess = amount - cash
    return AnnualSettlementSplit(cash, excess if other_half_action == 'carry' else Fraction(),
                                 excess if other_half_action == 'zero' else Fraction(),
                                 Fraction(), full_annual_entitlement, False)
