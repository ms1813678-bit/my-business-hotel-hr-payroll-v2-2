from datetime import date

import pytest

from app.leave_release_preview import annual_release_preview


@pytest.mark.parametrize(('hire', 'release'), [
    (date(2026, 1, 1), date(2026, 2, 1)),
    (date(2026, 2, 25), date(2026, 4, 1)),
    (date(2026, 3, 15), date(2026, 5, 1)),
    (date(2026, 10, 29), date(2026, 12, 1)),
    (date(2026, 12, 1), date(2027, 1, 1)),
    (date(2026, 12, 29), date(2027, 2, 1)),
    (date(2028, 2, 1), date(2028, 3, 1)),
])
def test_complete_calendar_month_precedes_retroactive_release(hire, release):
    result = annual_release_preview(hire_date=hire, as_of=release,
                                    full_calendar_month_worked=True)
    assert result.earliest_release_date == release
    assert result.released
    assert result.retroactive_from == hire


def test_partial_february_never_releases_march_first():
    result = annual_release_preview(hire_date=date(2026, 2, 25), as_of=date(2026, 3, 1),
                                    full_calendar_month_worked=True)
    assert not result.released
    assert result.retroactive_from is None


@pytest.mark.parametrize('coverage', [None, False])
def test_unknown_or_interrupted_full_month_never_infers_eligibility(coverage):
    result = annual_release_preview(hire_date=date(2026, 2, 25), as_of=date(2026, 4, 1),
                                    full_calendar_month_worked=coverage)
    assert not result.released
    assert result.reason == 'full_calendar_month_evidence_unresolved'


def test_employment_evidence_not_truthy_string():
    with pytest.raises(TypeError):
        annual_release_preview(hire_date=date(2026, 1, 1), as_of=date(2026, 2, 1),
                               full_calendar_month_worked='yes')
