"""Pure validation of explicitly supplied leave policy, never inferred defaults.

Not a settings store, role grant or recalculation action. Unresolved calendar
and rolling-year choices must remain outside posted balances until confirmed.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext

from app.leave_day_calculations import PRECISION


@dataclass(frozen=True)
class LeavePolicy:
    tier30_by_insurance_years: bool
    tier30_by_age_50: bool
    annual_deduct_unpaid_absence: bool
    annual_deduct_sick: bool
    annual_year_basis: str
    cash_basis: str
    holiday_credit_during_sick: bool
    sick_alert_thresholds: tuple[int, ...]

    def __post_init__(self):
        for name in ('tier30_by_insurance_years', 'tier30_by_age_50',
                     'annual_deduct_unpaid_absence', 'annual_deduct_sick',
                     'holiday_credit_during_sick'):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f'{name} must be explicit boolean.')
        if self.annual_year_basis not in {'calendar', 'hire_anniversary'}:
            raise ValueError('Explicit annual year basis is required.')
        if self.cash_basis not in {'prorated', 'full_tier'}:
            raise ValueError('Explicit year-end entitlement basis is required.')
        if (not isinstance(self.sick_alert_thresholds, tuple) or not self.sick_alert_thresholds
                or any(isinstance(n, bool) or not isinstance(n, int) or n < 1
                       for n in self.sick_alert_thresholds)
                or len(set(self.sick_alert_thresholds)) != len(self.sick_alert_thresholds)):
            raise ValueError('Explicit unique positive sick thresholds are required.')


def annual_deductible_count(unpaid_days: int, sick_days: int, policy: LeavePolicy) -> int:
    for count in (unpaid_days, sick_days):
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError('Evidence counts must be nonnegative whole days.')
    return (unpaid_days if policy.annual_deduct_unpaid_absence else 0) + (
        sick_days if policy.annual_deduct_sick else 0)


def insurance_years(prior: Decimal | None, current: Decimal | None) -> Decimal | None:
    """Unknown insurance evidence stays unknown, not zero or service tenure."""
    for value in (prior, current):
        if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
            raise ValueError('Insurance evidence must be nonnegative Decimal or unknown.')
    if prior is None or current is None:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        return prior + current
