"""Pure tests (no database) for the calendar-year sick window helper, item 2z. py3.10 shim, not 3.11."""
from datetime import date, datetime, timedelta

import pytest

from app.leave_sick_rolling_window import ANCHOR, calendar_sick_alert_state, sick_year_window


def span(start, n):
    return {start + timedelta(days=i) for i in range(n)}


def test_window_is_the_calendar_year_of_the_as_of_date_including_leap_years():
    assert sick_year_window(date(2026, 11, 26)) == (date(2026, 1, 1), date(2026, 12, 31))
    assert sick_year_window(date(2026, 1, 1)) == sick_year_window(date(2026, 12, 31))
    s, e = sick_year_window(date(2028, 2, 29))
    assert (s, e) == (date(2028, 1, 1), date(2028, 12, 31)) and (e - s).days + 1 == 366
    s, e = sick_year_window(date(2027, 6, 1))
    assert (e - s).days + 1 == 365
    for bad in ('2026-11-26', None, datetime(2026, 11, 26, 1)):
        with pytest.raises(TypeError):
            sick_year_window(bad)


def test_counter_resets_at_the_year_boundary():
    dec = span(date(2026, 10, 3), 90)                       # 2026-10-03..2026-12-31 (90 days)
    jan = span(date(2027, 1, 1), 5)
    employed = frozenset(dec | jan)
    s26, m26 = calendar_sick_alert_state(dec | jan, employed, date(2026, 12, 31), thresholds=(90, 180),
                                         certificate_flags={d: True for d in dec})
    assert s26.days == 90 and s26.reached_thresholds == (90,) and m26['anchor'] == ANCHOR
    assert m26['start'] == '2026-01-01' and m26['end'] == '2026-12-31' and m26['posting_allowed'] is False
    s27, _ = calendar_sick_alert_state(dec | jan, employed, date(2027, 1, 5), thresholds=(90, 180), certificate_flags={})
    assert s27.days == 5 and s27.reached_thresholds == () and s27.missing_certificate_days == 5
    # the same dates are counted by the year of the as-of date only, whatever day of that year it is
    assert calendar_sick_alert_state(dec | jan, employed, date(2026, 1, 1), thresholds=(90,), certificate_flags={})[0].days == 90


def test_thresholds_per_calendar_year_employment_filter_and_validation():
    year = span(date(2028, 1, 1), 180)                       # leap year, exactly 180 days
    employed = frozenset(year)
    st, _ = calendar_sick_alert_state(year, employed, date(2028, 12, 31), thresholds=(180, 90),
                                      certificate_flags={d: True for d in year})
    assert st.days == 180 and st.reached_thresholds == (90, 180) and st.missing_certificate_days == 0
    one_off, _ = calendar_sick_alert_state(year, frozenset(list(year)[:-1]), date(2028, 6, 1), thresholds=(180,), certificate_flags={})
    assert one_off.days == 179 and one_off.reached_thresholds == ()
    edge = {date(2026, 12, 31), date(2027, 1, 1)}
    st, _ = calendar_sick_alert_state(edge, frozenset(edge), date(2026, 7, 1), thresholds=(1,), certificate_flags={})
    assert st.days == 1
    for bad in ((), (0,), (90, 90), (True,)):
        with pytest.raises(ValueError):
            calendar_sick_alert_state(edge, frozenset(edge), date(2026, 7, 1), thresholds=bad, certificate_flags={})
