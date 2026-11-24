from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest

from app.leave_month_preview import preview_month_balances
from app.leave_policy_preview import LeavePolicy
from app.leave_read_model import RegisterEvidenceSummary


def arguments():
    return {
        'evidence': RegisterEvidenceSummary(False, 31, 0, 25,
            {'W': 2, 'H': 1, 'A': 1, 'S': 1, 'U': 1}, (), ()),
        'annual_tier': 15, 'month_start': date(2026, 1, 1),
        'employed': frozenset(date(2026, 1, 1) + timedelta(days=n) for n in range(31)),
        'holidays': {date(2026, 1, 7)}, 'sick_dates': {date(2026, 1, 2)},
        'opening_weekly': Decimal(-4), 'opening_holiday': Decimal(0), 'opening_annual': Decimal(0),
        'policy': LeavePolicy(True, True, True, False, 'calendar', 'prorated', False, (90, 180))}


def test_month_preview_composes_explicit_day_evidence_without_posting():
    args = arguments()
    result = preview_month_balances(**args)
    with localcontext() as context:
        context.prec = 50
        assert result.weekly_accrued == Decimal(29) / 7
        assert result.annual_accrued == Decimal(15) * 30 / (12 * 31)
    assert result.holiday_credited == 1
    assert result.holiday_balance == 0
    assert result.weekly_balance < 0
    assert args['evidence'].codes['W'] == 2


@pytest.mark.parametrize('changes', [
    {'unknown_days': 1}, {'conflicting_dates': (date(2026, 1, 1),)},
    {'service_evidence_missing': True}, {'present_days': 26}, {'employed_days': True},
])
def test_incomplete_or_conflicting_evidence_never_becomes_balance(changes):
    args = arguments()
    args['evidence'] = replace(args['evidence'], **changes)
    with pytest.raises(ValueError):
        preview_month_balances(**args)


@pytest.mark.parametrize('changes', [
    {'month_start': date(2026, 1, 2)},
    {'holidays': {date(2026, 2, 1)}},
    {'sick_dates': set()},
    {'employed': frozenset()},
])
def test_date_scope_and_count_mismatches_are_not_silently_clipped(changes):
    with pytest.raises(ValueError):
        preview_month_balances(**(arguments() | changes))
