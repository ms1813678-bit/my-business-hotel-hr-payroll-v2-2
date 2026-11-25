import pytest
from app.leave_annual_settlement_source import review_annual_settlement
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa


def test_actual_november_hire_keeps_specific_preservation_not_cash(prior_year):
    db, employee, kinds, args = prior_year
    reviewed = review_annual_settlement(db, source_selection={**args, 'leave_type_id': kinds['A']},
        full_annual_entitlement=15, other_half_action='carry')
    assert reviewed['plan']['remaining'] == '3/2'
    assert reviewed['plan']['cash_days'] == '0/1'
    assert reviewed['plan']['carried_days'] == '3/2'
    assert reviewed['plan']['late_year_preservation']
    assert not reviewed['plan']['cash_payment_committed'] and not reviewed['posting_allowed']
    assert not (db.new or db.dirty or db.deleted)


@pytest.mark.parametrize('entitlement', [True, 21, 30, 7.5])
def test_entitlement_cannot_be_caller_invented(prior_year, entitlement):
    db, employee, kinds, args = prior_year
    with pytest.raises(ValueError, match='verified earned-year tier'):
        review_annual_settlement(db, source_selection={**args, 'leave_type_id': kinds['A']},
            full_annual_entitlement=entitlement, other_half_action='carry')


def test_nonannual_type_cannot_take_annual_cash_rule(prior_year):
    db, employee, kinds, args = prior_year
    with pytest.raises(ValueError, match='actual annual leave type'):
        review_annual_settlement(db, source_selection=args, full_annual_entitlement=15, other_half_action='carry')


@pytest.mark.parametrize('prior_year,expected_cash,expected_carry', [
    ({'hire_date': __import__('datetime').date(2025, 1, 1), 'used_annual': 9}, '6/1', '0/1'),
    ({'hire_date': __import__('datetime').date(2025, 1, 1), 'used_annual': 6}, '15/2', '3/2'),
], indirect=['prior_year'])
def test_owner_examples_from_actual_full_year_earned_and_used_register(prior_year, expected_cash, expected_carry):
    db, employee, kinds, args = prior_year
    result = review_annual_settlement(db, source_selection={**args, 'leave_type_id': kinds['A']},
        full_annual_entitlement=15, other_half_action='carry')
    assert result['plan']['cash_days'] == expected_cash
    assert result['plan']['carried_days'] == expected_carry
    assert not result['plan']['late_year_preservation']
    assert not result['posting_allowed']
