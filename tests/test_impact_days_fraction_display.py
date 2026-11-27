from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace


def _filter():
    from app.leave_policy_impact_routes import policy_impact_router
    templates=SimpleNamespace(env=SimpleNamespace(filters={}))
    policy_impact_router(templates)
    return templates.env.filters['impact_days']


def test_impact_days_formats_fraction_on_any_python():
    shown=_filter()
    assert shown(Fraction(16,7))=='2.29'
    assert shown(Fraction(1,3))=='0.33'
    assert shown(Fraction(5,2))=='2.50'
    assert shown(Fraction(1,8))=='0.12'
    assert shown(Fraction(-16,7))=='-2.29'


def test_impact_days_decimal_unchanged():
    shown=_filter()
    assert shown(Decimal('2.285'))=='2.28'
    assert shown(Decimal('3'))=='3.00'
