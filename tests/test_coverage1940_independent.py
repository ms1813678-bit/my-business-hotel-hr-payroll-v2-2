"""Independent day-enumeration reference tests for app/leave_year_coverage.py."""
import random
from datetime import date, timedelta

import pytest

from app.leave_year_coverage import required_earning_months, validate_earning_month_coverage


def reference_months(source_year, service_periods):
    """Enumerate every day, derive the set of employed first-of-month months."""
    year_start, year_end = date(source_year, 1, 1), date(source_year, 12, 31)
    months = set()
    for start, end in service_periods:
        d = max(start, year_start)
        stop = year_end if end is None else min(end, year_end)
        if d > stop:
            continue
        while d <= stop:
            months.add(d.replace(day=1))
            d += timedelta(days=1)
    return tuple(sorted(months))


def make_source(source_year, baseline, service_periods):
    return dict(source_year=source_year, baseline=baseline,
                service_periods=service_periods, reviewed_on=date(source_year + 1, 1, 1))


@pytest.mark.parametrize('periods,expected_count', [
    # Leap February: continuous employment across it counts it exactly once.
    (((date(2024, 2, 1), date(2024, 3, 31)),), 2),
    # Leap February boundary slices: partial months still count whole months.
    (((date(2024, 2, 29), date(2024, 3, 1)),), 2),
    # Non-leap February must not over-extend into March on day 29 absence.
    (((date(2023, 2, 1), date(2023, 2, 28)),), 1),
])
def test_leap_february_matches_reference(periods, expected_count):
    day_year = periods[0][0].year
    assert len(reference_months(day_year, periods)) == expected_count
    actual = required_earning_months(**make_source(day_year, date(day_year, 1, 1), periods))
    assert actual == reference_months(day_year, periods)


def test_jan_dec_year_boundaries_clamped_to_source_year():
    periods = ((date(2023, 12, 15), date(2024, 1, 15)), (date(2024, 11, 1), None))
    source = make_source(2024, date(2023, 1, 1), periods)
    months = required_earning_months(**source)
    assert months == reference_months(2024, periods)
    assert months[0] == date(2024, 1, 1) and months[-1] == date(2024, 12, 1)


def test_gap_between_periods_excludes_only_gap_months():
    periods = ((date(2024, 1, 20), date(2024, 3, 10)), (date(2024, 6, 1), date(2024, 6, 30)))
    source = make_source(2024, date(2024, 1, 1), periods)
    assert required_earning_months(**source) == reference_months(2024, periods)
    assert date(2024, 5, 1) not in required_earning_months(**source)


def test_reordered_nonoverlapping_periods_match_reference():
    forward = ((date(2024, 2, 1), date(2024, 3, 15)), (date(2024, 9, 1), None))
    backward = (forward[1], forward[0])
    assert required_earning_months(**make_source(2024, date(2024, 1, 1), forward)) == \
        required_earning_months(**make_source(2024, date(2024, 1, 1), backward)) == \
        reference_months(2024, forward)


def test_duplicate_receipt_month_refused_valid_unique_accepted():
    periods = ((date(2024, 2, 10), date(2024, 3, 5)), (date(2024, 12, 1), date(2024, 12, 31)))
    source = make_source(2024, date(2024, 1, 1), periods)
    expected = reference_months(2024, periods)
    with pytest.raises(ValueError, match='Unique'):
        validate_earning_month_coverage(receipt_months=expected + (date(2024, 2, 1),), **source)
    assert validate_earning_month_coverage(receipt_months=expected, **source) == expected


def test_randomized_metamorphic_against_reference():
    rng = random.Random(1940)
    for _ in range(30):
        year = rng.choice([2023, 2024, 2025])
        pieces = []
        day = date(year, 1, 1)
        while day <= date(year, 12, 31):
            if rng.random() < 0.5:
                run = timedelta(days=rng.randint(1, 45))
                end = min(day + run - timedelta(days=1), date(year, 12, 31))
                pieces.append((day, end))
                day = end + timedelta(days=rng.randint(1, 20))
            else:
                day += timedelta(days=rng.randint(1, 20))
        if not pieces:
            pieces = [(date(year, 1, 1), None)]
        periods = tuple(pieces)
        source = make_source(year, date(year, 1, 1), periods)
        assert required_earning_months(**source) == reference_months(year, periods)
        if periods[0][0] == date(year, 1, 1):
            assert validate_earning_month_coverage(
                receipt_months=reference_months(year, periods), **source) == reference_months(year, periods)
