from datetime import date
from decimal import Decimal, localcontext

import pytest

from app.overtime_policy_preview import preview_overtime_day


def args():
    return {'work_date': date(2026,11,23),'basic_salary': Decimal(9000),
        'performance_bonus': Decimal(3000),'rate_divisor_hours': 8,'manual_hours': Decimal(2),
        'attendance_date': date(2026,11,23),'attendance_extra_hours': Decimal(2)}


@pytest.mark.parametrize('divisor,expected',[(8,Decimal(50)),(9,Decimal(400)/9),(12,Decimal(100)/3)])
def test_explicit_base_plus_performance_divisor30_then_day_hours(divisor,expected):
    with localcontext() as context:
        context.prec=50
        result=preview_overtime_day(**(args()|{'rate_divisor_hours':divisor}))
        assert result.hourly_rate==Decimal(12000)/30/divisor
        assert result.amount_before_rounding==result.hourly_rate*2
        assert result.review_issue is None


def test_other_bonus_categories_cannot_be_silently_included():
    with pytest.raises(TypeError):
        preview_overtime_day(**args(),occupancy_bonus=Decimal(9000))


def test_wrong_day_attendance_cannot_cover_request():
    with pytest.raises(ValueError,match='same day'):
        preview_overtime_day(**(args()|{'attendance_date':date(2026,11,22)}))


def test_missing_attendance_never_becomes_zero_or_approved():
    result=preview_overtime_day(**(args()|{'attendance_date':None,'attendance_extra_hours':None}))
    assert result.attendance_extra_hours is None
    assert result.variance_hours is None
    assert result.review_issue=='same_day_attendance_evidence_missing'


def test_manual_excess_needs_reason_then_still_needs_authorized_review():
    result=preview_overtime_day(**(args()|{'manual_hours':Decimal(3)}))
    assert result.variance_hours==1
    assert result.review_issue=='excess_manual_hours_require_reason_and_authorized_review'
    reviewed=preview_overtime_day(**(args()|{'manual_hours':Decimal(3),'override_reason':'Synthetic reason'}))
    assert reviewed.review_issue=='excess_manual_hours_require_authorized_review'


@pytest.mark.parametrize('value',[Decimal('NaN'),Decimal('Infinity'),Decimal(-1),True,1.5,'2'])
def test_bad_manual_hours_rejected(value):
    with pytest.raises(ValueError):
        preview_overtime_day(**(args()|{'manual_hours':value}))


@pytest.mark.parametrize('value',[True,0,7,10,'8',None])
def test_unknown_divisor_never_guessed_from_job_name(value):
    with pytest.raises(ValueError):
        preview_overtime_day(**(args()|{'rate_divisor_hours':value}))
