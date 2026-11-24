from datetime import date
from decimal import Decimal,localcontext
import pytest
from app.leave_late_year_preservation import describe_late_year_annual


def args(hire=date(2026,12,15)):
    return dict(employee_id=1,hire_date=hire,earning_year=2026,earned_annual=Decimal('0.68548387096774193548387096774193548387096774193548'),
        annual_used_from_that_earning_year=Decimal(0),release_evaluated_on=date(2026,12,31),
        full_calendar_month_worked=True,earning_year_attribution_confirmed=True)


@pytest.mark.parametrize('hire,released,first_release',[(date(2026,11,1),True,date(2026,12,1)),(date(2026,11,15),False,date(2027,1,1)),(date(2026,12,1),False,date(2027,1,1)),(date(2026,12,15),False,date(2027,2,1))])
def test_late_hire_year_end_preserves_without_accelerating_release(hire,released,first_release):
    a=args(hire);r=describe_late_year_annual(**a)
    assert r.exception_applies and r.preserved_earned_remainder==a['earned_annual']
    assert r.earliest_release_date==first_release
    assert (r.released_part==a['earned_annual'])==released
    later=describe_late_year_annual(**{**a,'release_evaluated_on':first_release if first_release>=date(2026,12,31) else date(2027,1,1)})
    assert later.held_part==0 and later.released_part==a['earned_annual']
    assert r.following_year_end==date(2027,12,31)
    assert not r.posting_allowed and not r.establishes_spendable_credit and not r.cash_action_allowed


@pytest.mark.parametrize('change,issue',[({'hire_date':date(2025,11,1)},'not_covered_by_late_year_new_hire_exception'),({'earning_year_attribution_confirmed':False},'earned_year_attribution_unresolved_no_opening_inference'),({'annual_used_from_that_earning_year':Decimal(1)},'negative_remainder_requires_separate_debt_carry')])
def test_unknown_attribution_veteran_and_debt_are_not_silently_mapped(change,issue):
    r=describe_late_year_annual(**{**args(),**change})
    assert r.preserved_earned_remainder is None and issue in r.issues


def test_interrupted_first_full_month_stays_held_and_low_context_keeps_fraction():
    a={**args(),'release_evaluated_on':date(2027,2,1),'full_calendar_month_worked':False}
    r=describe_late_year_annual(**a)
    assert r.released_part==0 and r.held_part==a['earned_annual']
    with localcontext() as context:
        context.prec=4
        assert describe_late_year_annual(**a)==r


@pytest.mark.parametrize('hire,as_of,release',[(date(2026,11,30),date(2026,12,31),date(2027,1,1)),(date(2026,12,31),date(2027,1,31),date(2027,2,1))])
def test_last_day_hires_preserve_and_wait_for_real_calendar_release(hire,as_of,release):
    r=describe_late_year_annual(**{**args(hire),'release_evaluated_on':as_of,'full_calendar_month_worked':None})
    assert r.preserved_earned_remainder==args(hire)['earned_annual'] and r.released_part==0
    assert r.earliest_release_date==release
