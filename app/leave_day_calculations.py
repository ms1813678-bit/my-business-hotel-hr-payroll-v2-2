"""Pure day-only leave calculations, not enabled in routes or payroll.

No database writes, settings defaults, authorization decisions, notifications,
clock reads or financial values. Callers must supply confirmed employment/code
counts and policy inputs. Unresolved year-basis/rolling-year rules stay outside.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext

PRECISION = 50
ZERO = Decimal(0)


def _days(value: Decimal | int, *, allow_negative=False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError("Days must be Decimal or integer, never binary float.")
    result = Decimal(value)
    if not result.is_finite() or (not allow_negative and result < ZERO):
        raise ValueError("Invalid day count.")
    return result


def _count(value: int) -> Decimal:
    result = _days(value)
    if result != result.to_integral_value():
        raise ValueError("Date counts must be whole days.")
    return result


def weekly_accrued(employed_days: int, unpaid_days: int, sick_days: int) -> Decimal:
    """Counts must be disjoint and already restricted to employed dates."""
    employed, unpaid, sick = map(_count, (employed_days, unpaid_days, sick_days))
    if unpaid + sick > employed:
        raise ValueError("Excluded days exceed employed days.")
    with localcontext() as context:
        context.prec = PRECISION
        return (employed - unpaid - sick) / Decimal(7)


def annual_month_accrued(tier: int, days_in_month: int, employed_days: int,
                         deductible_days: int) -> Decimal:
    """Tier/deductible selection is external, never inferred here.

    Period inputs are a single already-selected month. Service gaps and end of
    employment are represented by employed_days. No six-month waiting period.
    """
    if isinstance(tier, bool) or not isinstance(tier, int) or tier not in (15, 21, 30):
        raise ValueError("Annual tier must be 15, 21 or 30 days.")
    if isinstance(days_in_month, bool) or days_in_month not in (28, 29, 30, 31):
        raise ValueError("Invalid calendar month length.")
    employed, deductible = map(_count, (employed_days, deductible_days))
    if employed > days_in_month or deductible > employed:
        raise ValueError("Invalid employed/deductible counts.")
    with localcontext() as context:
        context.prec = PRECISION
        return Decimal(tier) * (employed - deductible) / (Decimal(12) * days_in_month)


def day_balance(opening: Decimal, accrued: Decimal, used: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = PRECISION
        return _days(opening, allow_negative=True) + _days(accrued) - _days(used)


@dataclass(frozen=True)
class YearEndDays:
    cash_days: Decimal
    excess: Decimal
    negative_carry: Decimal


def year_end_days(remaining: Decimal, basis: Decimal) -> YearEndDays:
    """Split supplied entitlement basis; does not select a year or grant window.

    Negative balances carry, not negative cash. Excess expiry/grant timestamps
    require explicit workflow decisions and are intentionally not written here.
    """
    remaining, basis = _days(remaining, allow_negative=True), _days(basis)
    if remaining < ZERO:
        return YearEndDays(ZERO, ZERO, remaining)
    with localcontext() as context:
        context.prec = PRECISION
        cash = min(remaining, basis / Decimal(2))
        return YearEndDays(cash, remaining - cash, ZERO)


def resolved_code(existing: str | None, proposed: str, *, hr_override=False) -> str:
    """Pure preview resolution. Does not authorize an override or write a cell.

    W/H/A remain when S or U arrives; S remains against U. Other conflicting
    filled-cell edits require an explicit authorized HR workflow outside here.
    Empty imports must remain unresolved evidence, not be passed as attendance.
    """
    existing = existing.upper() if existing else None
    proposed = proposed.upper()
    allowed = {"W", "H", "A", "S", "U"}
    if proposed not in allowed or (existing is not None and existing not in allowed):
        raise ValueError("Unknown leave/absence code.")
    if existing is None or existing == proposed:
        return proposed
    if (existing in {"W", "H", "A"} and proposed in {"S", "U"}) or (existing == "S" and proposed == "U"):
        return existing
    if not hr_override:
        raise ValueError("Filled-cell override requires authorized HR confirmation.")
    return proposed


def employed_dates(period_start: date, period_end: date,
                   service_periods: list[tuple[date, date | None]]) -> frozenset[date]:
    """Clip supplied authoritative service intervals, inclusive of end-work day.

    Union prevents duplicate accrual for overlapping imported service intervals;
    no current-active flag, inferred hire/end date or clock is used.
    """
    if period_end < period_start:
        raise ValueError("Invalid report period.")
    result = set()
    for start, end in service_periods:
        if end is not None and end < start:
            raise ValueError("Invalid service period.")
        first, last = max(start, period_start), min(end or period_end, period_end)
        if first <= last:
            result.update(first + timedelta(days=offset) for offset in range((last-first).days + 1))
    return frozenset(result)


def holiday_credit(holiday_dates: set[date], employed: frozenset[date],
                   sick_dates: set[date], *, credit_during_sick: bool) -> Decimal:
    """Count HR-entered holiday dates once, restricted to service evidence.

    No posting, current-active inference or settings lookup. Caller explicitly
    supplies the sick-credit policy and approved sick dates after precedence.
    """
    if not isinstance(credit_during_sick, bool):
        raise TypeError("Sick-credit policy must be explicit boolean.")
    eligible = set(holiday_dates) & employed
    if not credit_during_sick:
        eligible -= set(sick_dates)
    return Decimal(len(eligible))


def sick_days_in_period(sick_dates: set[date], employed: frozenset[date],
                        start: date, end: date) -> int:
    """Count approved sick dates within an EXPLICIT supplied period.

    Does not choose365days versus12months or enforce maximum/pay percentages.
    """
    if end < start:
        raise ValueError("Invalid sick counter period.")
    return sum(start <= day <= end for day in set(sick_dates) & employed)


@dataclass(frozen=True)
class SickAlertState:
    days: int
    reached_thresholds: tuple[int, ...]
    missing_certificate_days: int


def sick_alert_state(sick_dates: set[date], employed: frozenset[date],
                     start: date, end: date, *, thresholds: tuple[int, ...],
                     certificate_flags: dict[date, bool | None]) -> SickAlertState:
    """Pure preview, not a notification sender or sick-leave limit.

    Caller supplies confirmed period and configured thresholds. Missing flags
    remain warnings; they never reject sick dates or consume another balance.
    """
    if not thresholds or any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in thresholds):
        raise ValueError("Sick alert thresholds must be explicit positive integers.")
    if len(set(thresholds)) != len(thresholds):
        raise ValueError("Duplicate sick alert thresholds.")
    days = sick_days_in_period(sick_dates, employed, start, end)
    considered = {day for day in set(sick_dates) & employed if start <= day <= end}
    if any(flag is not None and not isinstance(flag, bool) for flag in certificate_flags.values()):
        raise TypeError("Certificate flags must be boolean or unknown.")
    missing = sum(certificate_flags.get(day) is not True for day in considered)
    return SickAlertState(days, tuple(n for n in sorted(thresholds) if days >= n), missing)
