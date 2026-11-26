"""Item 2z (sick counter), isolated and read-only: the calendar-year window for the sick-days counter.

Source: Mo's WhatsApp voice note, 2026-11-26 10:36:04 EET (transcript: counted from 1 January to 31 December): the sick year is the calendar year. Not a
rolling 12 months and not the hire anniversary. This is a preview helper over the existing sick_alert_state with the
configured thresholds (defaults 90 and 180 per his prompt). It posts nothing, sends no alert, limits no sick leave and
is not wired anywhere. File name kept from the first package so the writer replaces it.
"""
from datetime import date

from app.leave_day_calculations import SickAlertState, sick_alert_state

ANCHOR = 'calendar_year_of_as_of'


def sick_year_window(as_of: date) -> tuple[date, date]:
    if not isinstance(as_of, date) or hasattr(as_of, 'hour'):
        raise TypeError('as_of must be a date.')
    return date(as_of.year, 1, 1), date(as_of.year, 12, 31)


def calendar_sick_alert_state(sick_dates: set[date], employed: frozenset[date], as_of: date, *,
                              thresholds: tuple[int, ...], certificate_flags: dict[date, bool | None]) -> tuple[SickAlertState, dict]:
    start, end = sick_year_window(as_of)
    state = sick_alert_state(sick_dates, employed, start, end, thresholds=thresholds, certificate_flags=certificate_flags)
    return state, {'anchor': ANCHOR, 'start': start.isoformat(), 'end': end.isoformat(), 'posting_allowed': False}
