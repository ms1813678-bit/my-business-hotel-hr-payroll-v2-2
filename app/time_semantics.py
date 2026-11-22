"""Elapsed-time semantics for Cairo wall-clock attendance values.

Attendance punch and schedule columns are naive datetimes by design: they
record what the local clock showed in Cairo. For worked-duration display we
want actual elapsed time, which differs from the wall-clock difference on the
two DST transition nights each year (Africa/Cairo: spring forward at 00:00 on
the last Friday of April, fall back at 24:00 on the last Thursday of
October; in 2026 the skipped hour is 2026-04-24 00:00-01:00 and the repeated
hour is 2026-10-29 23:00-24:00).

Rules:
- Aware datetimes subtract as absolute instants (UTC-based) directly.
- Naive datetimes are interpreted as Africa/Cairo local time. When both
  endpoints map to exactly one real instant, the result is true elapsed time.
- When either endpoint is ambiguous (repeated fall-back hour) or nonexistent
  (skipped spring-forward hour), the stored naive value cannot identify the
  intended instant. We never invent one: the plain wall-clock difference is
  returned, unchanged from historical behavior.

Payroll does not consume worked minutes: lateness and early-out are computed
from scheduled shift times, and overtime uses the HR-approved stored hours.
This helper feeds reports and the AI assistant only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models import CAIRO_TZ


def _offset_choices(value: datetime) -> set:
    return {
        value.replace(tzinfo=CAIRO_TZ, fold=0).utcoffset(),
        value.replace(tzinfo=CAIRO_TZ, fold=1).utcoffset(),
    }


def _is_unambiguous_real_local(value: datetime) -> bool:
    """True when the naive wall time identifies exactly one real Cairo instant."""
    if len(_offset_choices(value)) != 1:
        return False  # repeated fall-back hour: two possible instants
    aware = value.replace(tzinfo=CAIRO_TZ)
    round_tripped = aware.astimezone(timezone.utc).astimezone(CAIRO_TZ).replace(tzinfo=None)
    return round_tripped == value  # skipped spring-forward hour fails this


def local_elapsed_minutes_detail(start: datetime, end: datetime) -> tuple[int, str]:
    """(minutes, basis): basis is "exact" for true elapsed time and
    "wall_fallback" when a stored endpoint cannot be mapped to one real
    instant, so callers can mark the value as uncertain instead of claiming
    exact elapsed time."""
    if start.tzinfo is not None and end.tzinfo is not None:
        # Same-tzinfo subtraction ignores offsets, so convert to UTC first to
        # keep the result instant-based.
        return (
            int((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() // 60),
            "exact",
        )
    naive_start = start.replace(tzinfo=None)
    naive_end = end.replace(tzinfo=None)
    if _is_unambiguous_real_local(naive_start) and _is_unambiguous_real_local(naive_end):
        instant_start = naive_start.replace(tzinfo=CAIRO_TZ).astimezone(timezone.utc)
        instant_end = naive_end.replace(tzinfo=CAIRO_TZ).astimezone(timezone.utc)
        return int((instant_end - instant_start).total_seconds() // 60), "exact"
    return int((naive_end - naive_start).total_seconds() // 60), "wall_fallback"


def local_elapsed_minutes(start: datetime, end: datetime) -> int:
    """Actual elapsed minutes between two attendance datetimes (see module doc)."""
    return local_elapsed_minutes_detail(start, end)[0]


DST_WALL_FALLBACK_ISSUE = "dst_wall_clock_duration_uncertain"
