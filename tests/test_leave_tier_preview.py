from dataclasses import replace
from decimal import Decimal

from app.leave_policy_preview import LeavePolicy
from app.leave_tier_preview import annual_tier_preview


def policy():
    return LeavePolicy(True, True, True, False, 'calendar', 'prorated', False, (90, 180))


def test_first_second_and_qualified_tiers_use_explicit_evidence():
    assert annual_tier_preview(service_year=1, age_years=28, insured_years=Decimal(2), policy=policy()).tier == 15
    assert annual_tier_preview(service_year=2, age_years=28, insured_years=Decimal(2), policy=policy()).tier == 21
    assert annual_tier_preview(service_year=1, age_years=50, insured_years=None, policy=policy()).tier == 30
    assert annual_tier_preview(service_year=1, age_years=None, insured_years=Decimal(10), policy=policy()).tier == 30


def test_unknown_evidence_is_not_silently_under_tiered():
    result = annual_tier_preview(service_year=2, age_years=None, insured_years=None, policy=policy())
    assert result.tier == 21
    assert result.unresolved_evidence == ('age_years', 'insured_years')


def test_disabled_qualifiers_do_not_require_evidence():
    disabled = replace(policy(), tier30_by_age_50=False, tier30_by_insurance_years=False)
    result = annual_tier_preview(service_year=1, age_years=None, insured_years=None, policy=disabled)
    assert result.tier == 15
    assert not result.unresolved_evidence
