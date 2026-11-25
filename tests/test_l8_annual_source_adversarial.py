from datetime import date
from fractions import Fraction
import pytest
from app.leave_annual_settlement_source import review_annual_settlement
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa

FULL = date(2025, 1, 1)


def plan(prior_year, action='carry', entitlement=15):
    db, employee, kinds, args = prior_year
    result = review_annual_settlement(db, source_selection={**args, 'leave_type_id': kinds['A']},
        full_annual_entitlement=entitlement, other_half_action=action)
    assert not (db.new or db.dirty or db.deleted)
    return result


@pytest.mark.parametrize('prior_year,cash,carry', [
    ({'hire_date': FULL, 'used_annual': 0}, '15/2', '15/2'),
    ({'hire_date': FULL, 'used_annual': 7}, '15/2', '1/2'),
    ({'hire_date': FULL, 'used_annual': 8}, '7/1', '0/1'),
    ({'hire_date': FULL, 'used_annual': 15}, '0/1', '0/1'),
], indirect=['prior_year'])
def test_boundaries_cash_is_min_half_and_unused_and_everything_conserved(prior_year, cash, carry):
    result = plan(prior_year)
    p = result['plan']
    assert (p['cash_days'], p['carried_days'], p['cleared_days']) == (cash, carry, '0/1')
    assert Fraction(p['cash_days']) + Fraction(p['carried_days']) == Fraction(p['remaining'])
    assert p['ledger_delta'] == '0/1' and not p['cash_payment_committed'] and not p['operational_spending_allowed']


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_carry_does_not_cancel_cash_and_digest_is_stable(prior_year):
    a, b = plan(prior_year), plan(prior_year)
    assert a['reviewed_digest'] == b['reviewed_digest']
    assert a['plan']['cash_days'] == '15/2' and a['plan']['carried_days'] == '3/2'


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
@pytest.mark.parametrize('action', ['cash', 'none', '', None, True])
def test_unknown_second_half_action_refuses(prior_year, action):
    with pytest.raises(ValueError):
        plan(prior_year, action=action)


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_zero_option_is_only_explicit_and_clears_not_cashes(prior_year):
    p = plan(prior_year, action='zero')['plan']
    assert (p['cash_days'], p['carried_days'], p['cleared_days']) == ('15/2', '0/1', '3/2')
