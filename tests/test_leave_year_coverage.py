from datetime import date
import pytest
from app.leave_year_coverage import required_earning_months, validate_earning_month_coverage


def args(**changes):
    return dict(source_year=2025, baseline=date(2025, 2, 25),
        service_periods=((date(2025, 2, 25), None),), reviewed_on=date(2026, 1, 1)) | changes


def test_partial_hire_month_through_december_required_including_zero_earning_months():
    expected = tuple(date(2025, m, 1) for m in range(2, 13))
    assert required_earning_months(**args()) == expected
    assert validate_earning_month_coverage(receipt_months=expected, **args()) == expected
    with pytest.raises(ValueError, match='omit'):
        validate_earning_month_coverage(receipt_months=expected[:-1], **args())


def test_explicit_departure_gap_and_rehire_no_phantom_months():
    expected = (date(2025, 2, 1), date(2025, 3, 1), date(2025, 11, 1), date(2025, 12, 1))
    source = args(service_periods=((date(2025, 2, 25), date(2025, 3, 2)), (date(2025, 11, 30), None)))
    assert required_earning_months(**source) == expected


@pytest.mark.parametrize('change', [
    {'reviewed_on': date(2025, 12, 31)}, {'source_year': True}, {'source_year': 9999},
    {'baseline': date(2026, 1, 1)}, {'service_periods': ()},
    {'service_periods': ((date(2025, 2, 25), None), (date(2025, 3, 1), None))},
    {'service_periods': ((date(2025, 2, 25), date(2025, 3, 1)), (date(2025, 3, 1), None))},
    {'service_periods': ((date(2025, 3, 1), date(2025, 2, 1)),)},
])
def test_incomplete_or_overlapping_source_refuses(change):
    with pytest.raises(ValueError): required_earning_months(**args(**change))


@pytest.mark.parametrize('receipts', [
    (date(2025, 2, 1), date(2025, 2, 1)), (date(2024, 12, 1),),
    (date(2025, 2, 2),), (), [date(2025, 2, 1)],
])
def test_unverified_receipt_date_set_is_not_coverage(receipts):
    with pytest.raises(ValueError): validate_earning_month_coverage(receipt_months=receipts, **args())


def test_older_baseline_and_service_entire_year_all_twelve():
    assert len(required_earning_months(**args(baseline=date(2020, 1, 1),
        service_periods=((date(2020, 1, 1), None),)))) == 12
