"""Late-year new-hire annual preservation descriptor, no settlement or posting.

Consumes explicitly attributed earned-year amounts, not an all-time balance or
manual opening. Preservation never accelerates release or erases a balance.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal,localcontext

from app.leave_balance_rules import _bounded_amount
from app.leave_release_preview import annual_release_preview


@dataclass(frozen=True)
class LateYearAnnualPreservation:
    employee_id: int
    earning_year: int
    exception_applies: bool
    preserved_earned_remainder: Decimal | None
    released_part: Decimal | None
    held_part: Decimal | None
    following_year_start: date
    following_year_end: date
    earliest_release_date: date
    release_evaluated_on: date
    issues: tuple[str,...]
    posting_allowed: bool = False
    establishes_spendable_credit: bool = False
    cash_action_allowed: bool = False


def describe_late_year_annual(*,employee_id,hire_date,earning_year,earned_annual,
        annual_used_from_that_earning_year,release_evaluated_on,full_calendar_month_worked,
        earning_year_attribution_confirmed):
    """Explicit attribution and original hire date required, not inferred by month.

    Negative remainders need separate debt-carry treatment. Unknown attribution
    cannot turn a manual opening into this year's earned balance. This pure
    descriptor does not select ordinary year close, cash, grant or rehire rules.
    """
    if type(employee_id) is not int or employee_id<1 or type(earning_year) is not int or not 1<=earning_year<=9998:
        raise ValueError('Explicit employee and representable earning year required.')
    if type(hire_date) is not date or type(release_evaluated_on) is not date:
        raise ValueError('Explicit hire and release evaluation dates required.')
    if release_evaluated_on<date(earning_year,12,31):
        raise ValueError('Year-end preservation evidence is not yet evaluated.')
    if type(earning_year_attribution_confirmed) is not bool:
        raise TypeError('Explicit earning-year attribution status required.')
    for amount in (earned_annual,annual_used_from_that_earning_year):
        _bounded_amount(amount)
        if amount<0:raise ValueError('Explicit nonnegative earned/used days required.')
    if hire_date>release_evaluated_on:
        raise ValueError('Hire date cannot follow evaluation.')
    release=annual_release_preview(hire_date=hire_date,as_of=release_evaluated_on,
        full_calendar_month_worked=full_calendar_month_worked)
    next_start=date(earning_year+1,1,1);next_end=date(earning_year+1,12,31)
    applies=hire_date.year==earning_year and hire_date.month in (11,12)
    issues=[];preserved=released=held=None
    if not applies:
        issues.append('not_covered_by_late_year_new_hire_exception')
    elif not earning_year_attribution_confirmed:
        issues.append('earned_year_attribution_unresolved_no_opening_inference')
    else:
        with localcontext() as context:
            context.prec=max(50,max(earned_annual.adjusted(),annual_used_from_that_earning_year.adjusted())-
                min(earned_annual.as_tuple().exponent,annual_used_from_that_earning_year.as_tuple().exponent)+2)
            remaining=earned_annual-annual_used_from_that_earning_year
        if remaining<0:
            issues.append('negative_remainder_requires_separate_debt_carry')
        else:
            preserved=remaining
            released=remaining if release.released else Decimal(0)
            held=Decimal(0) if release.released else remaining
            if not release.released:issues.append(release.reason)
    return LateYearAnnualPreservation(employee_id,earning_year,applies,preserved,released,held,
        next_start,next_end,release.earliest_release_date,release_evaluated_on,tuple(issues))
