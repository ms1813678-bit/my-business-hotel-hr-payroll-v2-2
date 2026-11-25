from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
import pytest
from app.leave_year_attribution import plan_year_attribution


def plan(**changes):
    args = dict(ledger_id=1, employee_id=2, leave_type_id=2147483647,
        source_digest='a'*64, source_amount=Fraction(10, 7),
        allocations=((2025, Fraction(3, 7)), (2026, Decimal(1))),
        reviewed_on=date(2026, 11, 25), reason='Synthetic explicit year evidence')
    return plan_year_attribution(**(args | changes))


def test_exact_split_conserves_existing_amount_no_new_credit_all_type_ids():
    with localcontext() as ctx:
        ctx.prec = 2
        result = plan()
    assert sum((v for _, v in result.allocations), Fraction()) == Fraction(10, 7)
    assert result.ledger_delta == 0
    assert result.leave_type_id == 2147483647


def test_debt_is_separate_and_remains_negative():
    result = plan(source_amount=Fraction(-10, 7), allocations=((2025, Fraction(-3, 7)), (2026, Decimal(-1))))
    assert result.source_amount == Fraction(-10, 7) and result.ledger_delta == 0


@pytest.mark.parametrize('changes', [
    {'allocations': ((2026, Decimal(1)),)},
    {'allocations': ((2025, Fraction(10, 7)), (2025, Decimal(0)))},
    {'allocations': ((2027, Fraction(10, 7)),)},
    {'allocations': ((True, Fraction(10, 7)),)},
    {'allocations': ((2025, Fraction(-4, 7)), (2026, Decimal(2)))},
    {'source_amount': Fraction(-10, 7), 'allocations': ((2025, Fraction(4, 7)), (2026, Decimal(-2)))},
    {'source_amount': Decimal(0), 'allocations': ((2025, Decimal(-1)), (2026, Decimal(1)))},
    {'allocations': [(2026, Fraction(10, 7))]},
    {'source_digest': 'x'}, {'reason': ''}, {'ledger_id': True},
    {'source_amount': Decimal('NaN')},
])
def test_invalid_or_unconserved_source_refuses(changes):
    with pytest.raises(ValueError): plan(**changes)


def test_canonical_order_and_source_year_amount_identity_binding():
    a = plan()
    assert plan(allocations=tuple(reversed(a.allocations))).attribution_digest == a.attribution_digest
    assert plan(allocations=((2024, Fraction(3, 7)), (2026, Decimal(1)))).attribution_digest != a.attribution_digest
    assert plan(ledger_id=3).attribution_digest != a.attribution_digest
    assert plan(source_digest='b'*64).attribution_digest != a.attribution_digest


def test_large_coprime_allocation_sum_refuses_fast_not_quadratic():
    import random, time
    random.seed(1)
    allocs = tuple((2026 - i, Fraction(1, random.getrandbits(13000) | (1 << 12999) | 1)) for i in range(200))
    start = time.time()
    with pytest.raises(ValueError, match='resource bound|conserve'):
        plan(source_amount=Fraction(1), allocations=allocs)
    assert time.time() - start < 1.0   # Original 200-entry case measured near 8s; larger case not measured.
