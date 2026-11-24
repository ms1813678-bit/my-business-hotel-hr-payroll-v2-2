from decimal import Decimal

import pytest

from app.leave_recalculation_preview import preview_recalculation


def test_preview_keeps_full_precision_and_negatives_without_mutation():
    before = {(1, 'A'): Decimal('4.375'), (1, 'W'): Decimal(-2)}
    after = {(1, 'A'): Decimal('4.125'), (1, 'W'): Decimal(-2)}
    preview = preview_recalculation(before, after, source_digest='a' * 64)
    assert len(preview.changes) == 1
    assert preview.changes[0].delta == Decimal('-0.250')
    assert before[1, 'A'] == Decimal('4.375')
    assert after[1, 'W'] == Decimal(-2)


def test_digest_is_order_stable_but_source_and_values_bound():
    balances = {(2, 'H'): Decimal(1), (1, 'A'): Decimal(2)}
    reverse = dict(reversed(list(balances.items())))
    a = preview_recalculation(balances, balances, source_digest='a' * 64)
    b = preview_recalculation(reverse, reverse, source_digest='a' * 64)
    assert a.preview_digest == b.preview_digest
    assert a.preview_digest != preview_recalculation(balances, balances, source_digest='b' * 64).preview_digest
    changed = balances | {(1, 'A'): Decimal(3)}
    assert a.preview_digest != preview_recalculation(balances, changed, source_digest='a' * 64).preview_digest


def test_missing_row_is_not_treated_as_zero():
    with pytest.raises(ValueError, match='coverage changed'):
        preview_recalculation({(1, 'A'): Decimal(2)}, {}, source_digest='a' * 64)


@pytest.mark.parametrize(('key', 'amount'), [((True, 'A'), Decimal(1)),
    ((1, 'S'), Decimal(1)), ((1, 'A'), Decimal('NaN')), ((1, 'A'), 1.5)])
def test_unresolved_or_invalid_inputs_rejected(key, amount):
    with pytest.raises(ValueError):
        preview_recalculation({key: amount}, {key: amount}, source_digest='a' * 64)


def test_long_fraction_and_large_cancellation_are_exact_in_low_context():
    from decimal import localcontext, Inexact, Rounded
    tiny=Decimal('0.'+'0'*100+'7')
    large=Decimal('9'*150+'.'+'0'*100+'7')
    expected=Decimal('9'*150)
    with localcontext() as ctx:
        ctx.prec=4
        ctx.traps[Inexact]=True
        ctx.traps[Rounded]=True
        a=preview_recalculation({(1,'A'):Decimal(0)},{(1,'A'):tiny},source_digest='a'*64)
        b=preview_recalculation({(1,'A'):tiny},{(1,'A'):large},source_digest='a'*64)
    assert a.changes[0].delta==tiny and b.changes[0].delta==expected


@pytest.mark.parametrize('amount',[Decimal('1e5000'),Decimal('1e-5000')])
def test_resource_envelope_rejects_before_formatting_no_rounding(amount):
    with pytest.raises(ValueError,match='bound'):
        preview_recalculation({(1,'A'):Decimal(0)},{(1,'A'):amount},source_digest='a'*64)


@pytest.mark.parametrize('source',[None,1,True])
def test_non_string_digest_refuses(source):
    with pytest.raises(ValueError):
        preview_recalculation({(1,'A'):Decimal(0)},{(1,'A'):Decimal(0)},source_digest=source)
