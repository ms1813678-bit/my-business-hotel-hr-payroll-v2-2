"""PACK C / 3j. NEW tests pinning CURRENT behavior of overtime vs same-day attendance.
Source SHA 778196c. No core change. Pure tests need only stdlib. Diagnostic tests need the
app import chain (fastapi/pydantic/psycopg, Python 3.11) and SKIP otherwise. DB tests need
migrated PostgreSQL and SKIP otherwise. Skips are UNRAN, never passes.
"""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
import pytest
from app.overtime_policy_preview import preview_overtime_day

D = date(2026, 2, 2)


def prev(**kw):
    base = dict(work_date=D, basic_salary=Decimal(9000), performance_bonus=Decimal(3000),
                rate_divisor_hours=8, manual_hours=Decimal(2), attendance_date=D,
                attendance_extra_hours=Decimal(0))
    return preview_overtime_day(**(base | kw))


# ---- preview layer (pure) ----
def test_overtime_without_attendance_is_unresolved_not_zero():
    r = prev(attendance_date=None, attendance_extra_hours=None)
    assert r.attendance_extra_hours is None and r.variance_hours is None
    assert r.review_issue == 'same_day_attendance_evidence_missing'
    assert r.amount_before_rounding == r.hourly_rate * 2  # amount still computed, no block


def test_attendance_below_requirement_extra_zero_makes_all_manual_hours_excess():
    r = prev(attendance_extra_hours=Decimal(0), manual_hours=Decimal(2))
    assert r.variance_hours == 2
    assert r.review_issue == 'excess_manual_hours_require_reason_and_authorized_review'
    assert prev(override_reason='x').review_issue == 'excess_manual_hours_require_authorized_review'


def test_blank_reason_equals_no_reason():
    assert prev(override_reason='   ').review_issue == 'excess_manual_hours_require_reason_and_authorized_review'


def test_zero_manual_hours_with_zero_extra_has_no_issue():
    r = prev(manual_hours=Decimal(0))
    assert r.variance_hours == 0 and r.review_issue is None


def test_manual_below_extra_negative_variance_no_issue():
    r = prev(attendance_extra_hours=Decimal(3), manual_hours=Decimal(1))
    assert r.variance_hours == -2 and r.review_issue is None


def test_rest_day_has_no_concept_in_preview_signature():
    with pytest.raises(TypeError):
        prev(is_rest_day=True)


def test_extra_without_date_refused():
    with pytest.raises(ValueError, match='without source date'):
        prev(attendance_date=None, attendance_extra_hours=Decimal(1))


# ---- diagnostic layer (needs app import chain) ----
def _diag():
    try:
        from app.overtime_day_source import attendance_duration_diagnostic
        return attendance_duration_diagnostic
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f'UNRAN: app import chain unavailable: {type(exc).__name__}')


def row(**kw):
    base = dict(work_date=D, status='approved', scheduled_start_at=datetime(2026, 2, 2, 9),
                scheduled_end_at=datetime(2026, 2, 2, 17), approved_clock_in=datetime(2026, 2, 2, 9),
                approved_clock_out=datetime(2026, 2, 2, 19), worked_minutes=600)
    return SimpleNamespace(**(base | kw))


def test_diag_missing_row():
    assert _diag()(None, D) == (None, None, ('same_day_record_missing',))


def test_diag_above_schedule():
    s, e, i = _diag()(row(), D)
    assert (s, e, i) == (480, Decimal(2), ())


def test_diag_below_requirement_is_zero_extra_not_none():
    s, e, i = _diag()(row(approved_clock_out=datetime(2026, 2, 2, 15), worked_minutes=360), D)
    assert e == 0 and i == ()


def test_diag_exact_schedule_zero_extra():
    _, e, i = _diag()(row(approved_clock_out=datetime(2026, 2, 2, 17), worked_minutes=480), D)
    assert e == 0 and i == ()


def test_diag_rest_day_shape_no_schedule_is_unknown_with_issue():
    # No rest-day flag exists on the record; a day with no schedule endpoints is just 'unknown'.
    _, e, i = _diag()(row(scheduled_start_at=None, scheduled_end_at=None), D)
    assert e is None and 'schedule_endpoints_missing' in i


def test_diag_rest_day_worked_with_no_clock_is_unknown():
    _, e, i = _diag()(row(scheduled_start_at=None, scheduled_end_at=None,
                          approved_clock_in=None, approved_clock_out=None, worked_minutes=None), D)
    assert e is None and set(i) >= {'schedule_endpoints_missing', 'approved_endpoints_missing'}


def test_diag_unapproved_status_blocks():
    _, e, i = _diag()(row(status='ready_for_review'), D)
    assert e is None and 'attendance_not_approved' in i


def test_diag_wrong_day_raises():
    with pytest.raises(ValueError, match='same work date'):
        _diag()(row(work_date=date(2026, 2, 3)), D)
