"""Approved new-hire annual release calendar, no accrual/posting or expiry.

A partial hire month is earned but held. The first complete calendar work month
must finish before release, then prior eligible earned days are retroactive.
Employment gaps/leaving require explicit evidence, never active-flag inference.
"""
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class AnnualReleasePreview:
    earliest_release_date: date
    released: bool
    retroactive_from: date | None
    reason: str


def _next_month(start: date) -> date:
    if start.month == 12:
        return date(start.year + 1, 1, 1)
    return date(start.year, start.month + 1, 1)


def annual_release_preview(*, hire_date: date, as_of: date,
                           full_calendar_month_worked: bool | None) -> AnnualReleasePreview:
    """Caller attests employment covered the first full calendar month.

    This is not an attendance/payroll approval. Unknown or false employment
    coverage cannot release days. No annual carry/cash exception is selected.
    """
    if type(hire_date) is not date or type(as_of) is not date:
        raise TypeError('Explicit calendar dates required.')
    if full_calendar_month_worked is not None and type(full_calendar_month_worked) is not bool:
        raise TypeError('Full-month employment evidence must be boolean or unknown.')
    first_month = hire_date if hire_date.day == 1 else _next_month(hire_date)
    earliest = _next_month(first_month)
    if as_of < earliest:
        return AnnualReleasePreview(earliest, False, None, 'full_calendar_month_not_finished')
    if full_calendar_month_worked is not True:
        return AnnualReleasePreview(earliest, False, None, 'full_calendar_month_evidence_unresolved')
    return AnnualReleasePreview(earliest, True, hire_date, 'retroactive_earned_credit_eligible')
