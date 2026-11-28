"""Which payroll month a new bonus, deduction or advance payment belongs to.

Rule (Mo's notes of 27 Nov 2026 23:40, 23:41 and 23:51, read in meaning, relayed by Main): month M stays
open for entries until the 5th of the next month, including the whole 5th (Main's reading of "until the 5th";
not a literal quote). After that, a new entry about M goes into the current month with its reason and a
"retroactive for M" label. It is never refused or lost. A payroll run of M that is already approved or
closed is a hard lock: it can never pick up a later entry, so the entry moves on even before the 5th."""
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CAIRO_TZ, PayrollRun

CUTOFF_DAY = 5


def today_cairo() -> date:
    return datetime.now(CAIRO_TZ).date()


def first_of_month(value: date) -> date:
    return date(value.year, value.month, 1)


def next_month(value: date) -> date:
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def is_month_open(db: Session, month: date, today: date) -> bool:
    month = first_of_month(month)
    if today > date(next_month(month).year, next_month(month).month, CUTOFF_DAY):
        return False
    status = db.scalar(select(PayrollRun.status).where(PayrollRun.period_start == month).limit(1))
    return status not in ("approved", "closed")


def place_month(db: Session, wanted: date, today: date | None = None) -> tuple[date, date | None]:
    """Return (month to use, month it is retroactive for or None)."""
    today = today or today_cairo()
    wanted = first_of_month(wanted)
    current = first_of_month(today)
    if wanted > current or is_month_open(db, wanted, today):
        return wanted, None
    target = current
    while not is_month_open(db, target, today):
        target = next_month(target)
    return target, wanted


SALARY_CHANGE_CLOSED_MONTH_MESSAGE = (
    "لا يمكن تسجيل تغيير في الراتب الأساسي بتاريخ سريان داخل شهر رواتب مقفل. "
    "اختر تاريخًا في الشهر الحالي، وسجّل الفرق كمكافأة أو خصم بأثر رجعي."
)


def salary_change_blocked(db: Session, effective_date: date, today: date | None = None) -> bool:
    """A basic-salary change dated in an earlier month whose payroll is closed (Mo, 28 Nov 2026 11:51:19: block).

    "Closed" is the same rule as for entries: after the whole 5th of the next month, or when that month's run is
    approved or closed. The current month is never blocked, so HR is never left without a valid date
    (dates in the future are refused elsewhere)."""
    today = today or today_cairo()
    month = first_of_month(effective_date)
    if month >= first_of_month(today):
        return False
    return not is_month_open(db, month, today)
