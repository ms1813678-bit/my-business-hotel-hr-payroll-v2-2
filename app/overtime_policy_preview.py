"""Owner-requested overtime policy preview, not payroll posting or compliance.

The caller supplies the selected month's actual performance award and explicit
rate divisor. Divisors are rate inputs only, never a shift schedule. No premium,
attendance completion, override authority or payment is inferred here.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext


@dataclass(frozen=True)
class OvertimeDayPreview:
    work_date: date
    hourly_rate: Decimal
    requested_hours: Decimal
    attendance_extra_hours: Decimal | None
    variance_hours: Decimal | None
    amount_before_rounding: Decimal
    review_issue: str | None


def _nonnegative(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError('Explicit finite nonnegative Decimal required.')
    return value


def preview_overtime_day(*, work_date: date, basic_salary: Decimal,
                         performance_bonus: Decimal, rate_divisor_hours: int,
                         manual_hours: Decimal, attendance_date: date | None,
                         attendance_extra_hours: Decimal | None,
                         override_reason: str | None = None) -> OvertimeDayPreview:
    if type(work_date) is not date:
        raise ValueError('Explicit work date required.')
    if type(rate_divisor_hours) is not int or rate_divisor_hours not in {8, 9, 12}:
        raise ValueError('Explicit permitted hourly-rate divisor required.')
    basic, bonus, hours = map(_nonnegative, (basic_salary, performance_bonus, manual_hours))
    # Attendance from another day cannot be silently used as the comparison.
    if attendance_date is not None and attendance_date != work_date:
        raise ValueError('Attendance comparison must refer to the same day.')
    extra = None if attendance_extra_hours is None else _nonnegative(attendance_extra_hours)
    if extra is not None and attendance_date is None:
        raise ValueError('Attendance amount without source date is unresolved.')
    if override_reason is not None and not isinstance(override_reason, str):
        raise TypeError('Override reason must be text or unknown.')
    with localcontext() as context:
        context.prec = 50
        rate = (basic + bonus) / Decimal(30) / Decimal(rate_divisor_hours)
        variance = None if extra is None else hours - extra
        amount = rate * hours
    issue = None
    if extra is None:
        issue = 'same_day_attendance_evidence_missing'
    elif variance > 0 and not (override_reason or '').strip():
        issue = 'excess_manual_hours_require_reason_and_authorized_review'
    elif variance > 0:
        issue = 'excess_manual_hours_require_authorized_review'
    return OvertimeDayPreview(work_date, rate, hours, extra, variance, amount, issue)
