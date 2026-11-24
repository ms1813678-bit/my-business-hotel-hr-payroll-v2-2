from datetime import date
from decimal import Decimal
from dataclasses import replace
import pytest
from app.pay_basis_preview import preview_pay_basis


def inputs():
    return dict(month_start=date(2026,2,1),basic_salary=Decimal(6000),performance_bonus=Decimal(3000),attendance_bonus=Decimal(100),occupancy_bonus=Decimal(200),other_bonus=Decimal(300),staff_reward_amount=Decimal(400),cashout_days=Decimal(3),overtime_divisor_hours=8,manual_overtime_hours=Decimal(2))


@pytest.mark.parametrize('month,days',[(date(2026,2,1),28),(date(2024,2,1),29),(date(2100,2,1),28),(date(2000,2,1),29),(date(2026,4,1),30),(date(2026,1,1),31)])
def test_actual_month_vs_fixed_special_basis(month,days):
    r=preview_pay_basis(**{**inputs(),'month_start':month})
    assert r.ordinary_month_days==days
    assert abs(r.ordinary_basic_daily-Decimal(6000)/Decimal(days))<Decimal('1e-24')
    assert r.cashout_daily==300 and r.cashout_amount_unrounded==900
    assert r.overtime_hourly==Decimal('37.5') and r.overtime_amount_unrounded==75
    assert not r.posting_allowed and not r.establishes_cashout_eligibility
    with pytest.raises(ValueError):replace(r,posting_allowed=True)


def test_excluded_bonus_changes_full_digest_not_special_basis():
    a=preview_pay_basis(**inputs());b=preview_pay_basis(**{**inputs(),'other_bonus':Decimal(9999)})
    assert a.special_basis==b.special_basis==9000
    assert a.source_digest!=b.source_digest
    assert a.excluded_bonus_total!=b.excluded_bonus_total


@pytest.mark.parametrize('value',[Decimal('NaN'),Decimal('Infinity'),Decimal(-1),True,3,'3',None])
def test_real_bounded_helper_rejects_invalid_amounts(value):
    with pytest.raises(ValueError):preview_pay_basis(**{**inputs(),'cashout_days':value})
