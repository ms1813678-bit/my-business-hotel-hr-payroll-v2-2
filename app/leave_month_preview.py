"""Pure explicit-month accrual preview, not payroll or ledger integration.

Unresolved register days prevent a balance result. Callers must supply an
already confirmed report period, annual tier, opening balances, employment and
holiday evidence. This module chooses no accounting year, cycle or operator.
"""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.leave_day_calculations import (
    annual_month_accrued,
    day_balance,
    holiday_credit,
    weekly_accrued,
)
from app.leave_policy_preview import LeavePolicy, annual_deductible_count
from app.leave_read_model import RegisterEvidenceSummary


@dataclass(frozen=True)
class MonthBalancePreview:
    """Earned-only projection, NOT released/usable credit or posting authority.

    New-hire release timing awaits its exact calendar-month rule. No field in
    this preview establishes that an employee can spend accrued annual days.
    """
    weekly_accrued: Decimal
    holiday_credited: Decimal
    annual_accrued: Decimal
    weekly_balance: Decimal
    holiday_balance: Decimal
    annual_balance: Decimal


def preview_month_balances(*, evidence: RegisterEvidenceSummary, annual_tier: int,
                           month_start: date, employed: frozenset[date],
                           holidays: set[date], sick_dates: set[date],
                           opening_weekly: Decimal, opening_holiday: Decimal,
                           opening_annual: Decimal, policy: LeavePolicy) -> MonthBalancePreview:
    """Opening balances are supplied once, not guessed from another ledger.

    W and A accrual here are for this explicitly supplied month, not cumulative
    year-to-date. Caller remains responsible for selecting and binding the
    exact evidence period before future authorized posting.
    """
    if month_start.day != 1:
        raise ValueError('Explicit first day of the selected month required.')
    days_in_month = monthrange(month_start.year, month_start.month)[1]
    if any((day.year, day.month) != (month_start.year, month_start.month)
           for day in employed | holidays | sick_dates):
        raise ValueError('Date evidence must belong to the selected month.')
    if evidence.service_evidence_missing:
        raise ValueError('Employment evidence is missing.')
    if evidence.unknown_days or evidence.conflicting_dates:
        raise ValueError('Resolve unknown or conflicting register evidence first.')
    expected_codes = {'W', 'H', 'A', 'S', 'U'}
    if set(evidence.codes) != expected_codes:
        raise ValueError('Complete normalized day-code counts are required.')
    counts = [evidence.employed_days, evidence.present_days, *evidence.codes.values()]
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts):
        raise ValueError('Evidence counts must be nonnegative integers.')
    if evidence.present_days + sum(evidence.codes.values()) != evidence.employed_days:
        raise ValueError('Register coverage does not reconcile to employed days.')
    if len(employed) != evidence.employed_days:
        raise ValueError('Employment date coverage does not reconcile.')
    if not sick_dates <= employed or len(sick_dates) != evidence.codes['S']:
        raise ValueError('Sick date coverage does not reconcile.')
    weekly = weekly_accrued(evidence.employed_days, evidence.codes['U'], evidence.codes['S'])
    annual = annual_month_accrued(annual_tier, days_in_month, evidence.employed_days,
        annual_deductible_count(evidence.codes['U'], evidence.codes['S'], policy))
    holiday = holiday_credit(holidays, employed, sick_dates,
                            credit_during_sick=policy.holiday_credit_during_sick)
    return MonthBalancePreview(weekly, holiday, annual,
        day_balance(opening_weekly, weekly, Decimal(evidence.codes['W'])),
        day_balance(opening_holiday, holiday, Decimal(evidence.codes['H'])),
        day_balance(opening_annual, annual, Decimal(evidence.codes['A'])))
