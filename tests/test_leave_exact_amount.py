from decimal import Decimal, localcontext
from fractions import Fraction
from types import SimpleNamespace
import pytest
from app.leave_exact_amount import exact_sum, exact_text, display_days, ledger_amount, terminating_decimal


def test_mixed_rational_credits_never_drop_or_approximate():
    with localcontext() as context:
        context.prec = 2
        assert exact_sum([Fraction(1,7)] * 7 + [Decimal('0.123456789')]) == Decimal('1.123456789')
        assert exact_sum([Fraction(1,7), Decimal('0.1')]) == Fraction(17,70)
        assert exact_text(Fraction(1,7)) == '1/7'
        assert terminating_decimal(Fraction(1,124)) is None
        assert terminating_decimal(Fraction(1,40)) == Decimal('.025')


@pytest.mark.parametrize('values,expected', [([], '0'), (['1.00','2.0'], '3.00'), (['-0.00'], '0.00'), (['1E-8','-0.00000001'], '0.00000000')])
def test_finite_legacy_scale_preserved(values, expected):
    assert exact_text(exact_sum(map(Decimal,values))) == expected


def test_rational_row_exclusive_and_reduced():
    row=SimpleNamespace(days_delta=None,exact_numerator=Decimal(1),exact_denominator=Decimal(7))
    assert ledger_amount(row)==Fraction(1,7)
    for changed in [{'days_delta':Decimal('.142857')},{'exact_denominator':None},{'exact_denominator':Decimal(0)}, {'exact_numerator':Decimal(2),'exact_denominator':Decimal(14)}]:
        invalid=SimpleNamespace(**(vars(row)|changed))
        with pytest.raises(ValueError): ledger_amount(invalid)


@pytest.mark.parametrize('value,expected',[(Fraction(1,7),'0.14'),(Fraction(-1,7),'-0.14'),(Decimal('1.005'),'1.00'),(Decimal('1.015'),'1.02'),(Decimal('-0.001'),'0.00')])
def test_display_is_separate_exact_integer_rounding(value,expected):
    assert display_days(value)==expected
    assert exact_text(value) != ''


def test_numeric_identity_scale_independent_and_storage_canonicalizes_finite():
    from app.leave_exact_amount import canonical_amount, storage_amount
    assert canonical_amount(Decimal('2.00'))==canonical_amount(Fraction(2,1))=='2/1'
    assert storage_amount(Fraction(1,2))==dict(days_delta=Decimal('.5'),exact_numerator=None,exact_denominator=None)
    assert storage_amount(Fraction(1,7))==dict(days_delta=None,exact_numerator=Decimal(1),exact_denominator=Decimal(7))


def test_incremental_bound_refuses_large_coprime_denominators_early():
    import time
    values=[Fraction(1,10**3990+2*k+1) for k in range(300)]
    start=time.monotonic()
    with pytest.raises(ValueError,match='bound'): exact_sum(values)
    assert time.monotonic()-start<1
