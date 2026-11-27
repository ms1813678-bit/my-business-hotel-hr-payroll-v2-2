from decimal import Decimal
from fractions import Fraction
import pytest


def test_exact_approval_days_accepts_exact_rationals_without_rounding():
    from app.leave_reconciliation_editor_routes import exact_approval_days
    assert exact_approval_days(Fraction(16,7))=='16/7'
    assert exact_approval_days(Fraction(-1,3))=='-1/3'
    assert exact_approval_days(Fraction(5,2))=='2.5'
    assert exact_approval_days(Decimal('2.50'))=='2.5'
    for bad in (float('nan'),1.5,'2',None,Decimal('NaN'),Decimal('Infinity')):
        with pytest.raises(ValueError):
            exact_approval_days(bad)


def test_approval_rounded_days_only_for_non_terminating_rationals():
    from app.leave_reconciliation_editor_routes import approval_rounded_days
    assert approval_rounded_days(Fraction(16,7))=='2.29'
    assert approval_rounded_days(Fraction(-1,3))=='-0.33'
    assert approval_rounded_days(Fraction(5,2))==''
    assert approval_rounded_days(Decimal('2.285'))==''


def test_new_approval_note_has_all_three_languages():
    from app.i18n import EN,ZH
    key='الرقم بعد ≈ للعرض فقط؛ المعتمد هو القيمة الدقيقة.'
    assert EN[key] and ZH[key]
