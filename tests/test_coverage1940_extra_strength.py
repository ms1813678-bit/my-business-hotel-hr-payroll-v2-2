from datetime import date
import pytest
from app.leave_year_coverage import required_earning_months, validate_earning_month_coverage


def src(periods, baseline=date(2024, 1, 1)):
    return dict(source_year=2024, baseline=baseline, service_periods=periods, reviewed_on=date(2025, 1, 1))


def test_overlap_and_same_day_adjacency_refused_next_day_accepted():
    with pytest.raises(ValueError, match='Overlapping'):
        required_earning_months(**src(((date(2024, 1, 1), date(2024, 2, 10)), (date(2024, 2, 10), date(2024, 3, 1)))))
    assert required_earning_months(**src(((date(2024, 1, 1), date(2024, 2, 9)), (date(2024, 2, 10), date(2024, 3, 1))))) == (
        date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1))


def test_baseline_truncates_first_year_to_baseline_month():
    months = required_earning_months(**src(((date(2023, 1, 1), None),), baseline=date(2024, 7, 20)))
    assert months[0] == date(2024, 7, 1) and months[-1] == date(2024, 12, 1) and len(months) == 6


def test_receipt_months_must_be_a_tuple():
    periods = ((date(2024, 1, 1), date(2024, 2, 1)),)
    with pytest.raises(ValueError, match='Unique'):
        validate_earning_month_coverage(receipt_months=[date(2024, 1, 1), date(2024, 2, 1)], **src(periods))
