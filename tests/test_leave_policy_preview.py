from dataclasses import replace
from decimal import Decimal

import pytest

from app.leave_policy_preview import LeavePolicy, annual_deductible_count, insurance_years


def explicit_policy():
    return LeavePolicy(True, True, True, False, 'calendar', 'prorated', False, (90, 180))


def test_confirmed_default_shape_is_explicit_not_inferred():
    policy = explicit_policy()
    assert annual_deductible_count(7, 7, policy) == 7
    assert annual_deductible_count(7, 7, replace(policy, annual_deduct_sick=True)) == 14
    assert annual_deductible_count(7, 7, replace(policy, annual_deduct_unpaid_absence=False)) == 0
    with pytest.raises(TypeError):
        LeavePolicy()


@pytest.mark.parametrize('changes', [{'tier30_by_age_50': 'yes'}, {'annual_year_basis': 'guess'},
    {'cash_basis': 'money'}, {'sick_alert_thresholds': (90, 90)}, {'sick_alert_thresholds': ()}])
def test_bad_policy_does_not_become_defaults(changes):
    with pytest.raises((TypeError, ValueError)):
        replace(explicit_policy(), **changes)


def test_unknown_insurance_does_not_become_zero_or_tenure():
    assert insurance_years(None, Decimal(2)) is None
    assert insurance_years(Decimal(8), None) is None
    assert insurance_years(Decimal('8.25'), Decimal('1.75')) == Decimal('10.00')
    with pytest.raises(ValueError):
        insurance_years(Decimal(-1), Decimal(2))


def test_insurance_sum_near_tier_threshold_is_not_rounded_by_ambient_context():
    from decimal import localcontext

    from app.leave_tier_preview import annual_tier_preview
    with localcontext() as context:
        context.prec = 6
        total = insurance_years(Decimal('9.999999999999999999999999999'),
                                Decimal('0.0000000000000000000000000001'))
        assert total == Decimal('9.9999999999999999999999999991')
        assert annual_tier_preview(service_year=2, age_years=28,
            insured_years=total, policy=explicit_policy()).tier == 21
