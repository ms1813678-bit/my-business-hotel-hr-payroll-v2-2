from decimal import Decimal,localcontext
from fractions import Fraction
import pytest
from app.leave_exact_month import calculate_exact_month,exact_decimal


def test_repeating_weekly_and_partial_annual_remain_rational_under_tiny_context():
    with localcontext() as context:
        context.prec=2
        result=calculate_exact_month(tier=15,days_in_month=31,employed_days=1,unpaid_days=0,sick_days=0,
            annual_deduct_unpaid=True,annual_deduct_sick=False,holiday_days=0)
        assert result.weekly==Fraction(1,7) and result.annual==Fraction(5,124)
        assert result.weekly*7==1 and result.annual*124==5
        assert exact_decimal(Decimal('0.0000000000000000000000000000000000000001'))==Fraction(1,10**40)


@pytest.mark.parametrize('days',[28,29,30,31])
def test_full_month_annual_exact_and_weekly_adds_without_context_rounding(days):
    result=calculate_exact_month(tier=21,days_in_month=days,employed_days=days,unpaid_days=0,sick_days=0,
        annual_deduct_unpaid=True,annual_deduct_sick=False,holiday_days=1)
    assert result.annual==Fraction(7,4) and result.weekly==Fraction(days,7) and result.holiday==1
