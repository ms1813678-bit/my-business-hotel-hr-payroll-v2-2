from datetime import date
from decimal import Decimal

import pytest

from app.leave_carry_grant import CarryGrant, grant_window_contains


@pytest.mark.parametrize('start,end', [(date(2026, 4, 5), date(2026, 7, 13)),
                                      (date(2026, 12, 20), date(2027, 3, 2))])
def test_explicit_grant_dates_not_january_default(start, end):
    grant = CarryGrant(1, 'A', Decimal('1.234567890123456789'), start, start, end, 'Synthetic')
    assert grant_window_contains(grant, start)
    assert grant_window_contains(grant, end)
    assert not grant_window_contains(grant, date(2028, 1, 1))
    assert grant.days == Decimal('1.234567890123456789')
    # Querying after the end never mutates or erases the grant.
    assert grant.available_through == end


@pytest.mark.parametrize('days', [True, 1.1, Decimal(0), Decimal(-1), Decimal('NaN')])
def test_no_inferred_grant_amount(days):
    with pytest.raises(ValueError):
        CarryGrant(1, 'W', days, date(2026, 4, 1), date(2026, 4, 1), date(2026, 5, 1), 'Synthetic')
