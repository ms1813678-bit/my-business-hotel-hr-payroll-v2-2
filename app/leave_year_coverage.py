"""Exact calendar coverage plan for year close, no receipt or posting authority."""
from datetime import date


def required_earning_months(*, source_year, baseline, service_periods, reviewed_on):
    """Return every employed source month from an explicit baseline through year end.

    Service gaps are allowed only when explicit, not inferred from absent monthly
    receipts. The original baseline may truncate the first year; opening-year
    attribution remains a separate proof. Every service overlap is refused.
    """
    if (type(source_year) is not int or not 1 <= source_year <= 9998
            or type(baseline) is not date or baseline > date(source_year, 12, 31)
            or type(reviewed_on) is not date or reviewed_on < date(source_year + 1, 1, 1)):
        raise ValueError('Explicit closed source year, baseline and review date required.')
    if type(service_periods) is not tuple or not 1 <= len(service_periods) <= 20000:
        raise ValueError('Explicit bounded service evidence required.')
    periods = []
    for item in service_periods:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError('Explicit service start/end required.')
        start, end = item
        if type(start) is not date or (end is not None and (type(end) is not date or end < start)):
            raise ValueError('Valid explicit service dates required.')
        periods.append((start, end))
    periods.sort(key=lambda x: x[0])
    for previous, current in zip(periods, periods[1:]):
        if previous[1] is None or current[0] <= previous[1]:
            raise ValueError('Overlapping service evidence refuses year close.')
    first, last = max(baseline, date(source_year, 1, 1)), date(source_year, 12, 31)
    months = set()
    for start, end in periods:
        start, end = max(first, start), min(last, end or last)
        if start > end:
            continue
        month = start.replace(day=1)
        while month <= end:
            months.add(month)
            if month.month == 12:
                break
            month = month.replace(month=month.month + 1)
    return tuple(sorted(months))


def validate_earning_month_coverage(*, receipt_months, **source):
    expected = required_earning_months(**source)
    if (type(receipt_months) is not tuple
            or any(type(m) is not date or m.day != 1 or m.year != source['source_year'] for m in receipt_months)
            or len(set(receipt_months)) != len(receipt_months)):
        raise ValueError('Unique first-of-month source-year receipt dates required.')
    if set(receipt_months) != set(expected):
        raise ValueError('Year earning receipts omit or exceed explicit employed-month coverage.')
    return expected
