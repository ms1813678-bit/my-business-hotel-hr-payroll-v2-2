"""L8 read-only regression: explanation layer must carry exact accruals, same as the month kernel."""
import pytest
from datetime import date
from fractions import Fraction

from app.leave_balance_explanation import explain_selected_balance
from tests.test_leave_source_reconciliation import setup_source
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_department_workbook_store import context  # noqa


def test_explanation_earned_equals_exact_month_kernel_fraction(history_context):
    db, e, args = setup_source(history_context)   # hire 2026-02-25, 4 employed days of 28, tier 15
    x = explain_selected_balance(db, release_evaluated_on=date(2026, 2, 28), **args)
    rows = {r.code: r for r in x.rows}
    assert Fraction(rows['A'].earned) == Fraction(15 * 4, 12 * 28)   # 5/28
    assert Fraction(rows['W'].earned) == Fraction(4, 7)
    assert Fraction(rows['A'].held_earned) == Fraction(rows['A'].earned)


from tests.test_leave_month_earning import monthly, post  # noqa: E402,F401


def test_projected_scenario_matches_posted_receipt_value_exactly_but_stays_unposted_label(monthly):
    """Projected scenario and posted receipt are different sources. Same inputs must give the same
    exact value, and the explanation must still refuse to call itself a balance or posting."""
    db, e, kinds, args = monthly
    sel = args(date(2026, 2, 1), date(2026, 2, 28))
    _review, result = post(db, sel)
    assert result['held_annual'] == '5/28'
    x = explain_selected_balance(
        db, selection_ids=(sel['selection_id'],), department_id=e.department_id,
        month_start=sel['month_start'], release_evaluated_on=sel['evaluated_on'],
        current_insurance_years=sel['current_insurance_years'],
        age_evaluation_dates=sel['age_evaluation_dates'],
        actor_id=sel['actor_id'], session_id=sel['session_id'],
        session_version=sel['session_version'], expected_epoch=sel['expected_epoch'])
    rows = {r.code: r for r in x.rows}
    assert Fraction(rows['A'].held_earned) == Fraction(result['held_annual'])
    assert Fraction(rows['W'].earned) == Fraction(4, 7)
    assert not x.posting_allowed and not x.establishes_spendable_credit
    assert rows['A'].current_balance is None and rows['A'].available_balance is None


import random
from decimal import Decimal, localcontext
from math import lcm

from app.leave_day_calculations import annual_month_accrued, weekly_accrued
import app.leave_source_reconciliation as recon


def test_annual_denominator_constant_is_twelve_times_lcm_of_month_lengths():
    assert recon._EXACT_DENOMINATOR == {'A': 12 * lcm(28, 29, 30, 31), 'W': 7, 'H': 1}


def test_reconstruction_equals_exact_sum_for_the_real_legacy_formulas_across_random_years():
    """Same accumulation as department_workbook_projection (prec 50, month by month), 3000 random years."""
    rng = random.Random(8)
    for _ in range(3000):
        a_dec, w_dec, a_exact, w_exact = Decimal(0), Decimal(0), Fraction(0), Fraction(0)
        with localcontext() as ctx:
            ctx.prec = 50
            for month in range(1, rng.randint(2, 12) + 1):
                dim = rng.choice((28, 29, 30, 31))
                employed = rng.randint(0, dim)
                unpaid = rng.randint(0, employed)
                sick = rng.randint(0, employed - unpaid)
                tier = rng.choice((15, 21, 30))
                deduct = rng.randint(0, employed)
                w_dec += weekly_accrued(employed, unpaid, sick)
                a_dec += annual_month_accrued(tier, dim, employed, deduct)
                w_exact += Fraction(employed - unpaid - sick, 7)
                a_exact += Fraction(tier * (employed - deduct), 12 * dim)
        assert recon._exact_from_projection_decimal(a_dec, 'A') == a_exact
        assert recon._exact_from_projection_decimal(w_dec, 'W') == w_exact


@pytest.mark.parametrize('value,code', [
    (Decimal('0.17857142857142857142857142857142857142857142857143') + Decimal('1e-30'), 'A'),  # off structure
    (Decimal('-1'), 'A'), (Decimal('NaN'), 'A'), (Decimal('1.5'), 'H'), (Decimal('1'), 'X'), (1, 'A'),
])
def test_values_outside_the_accrual_denominator_structure_refuse(value, code):
    with pytest.raises(ValueError):
        recon._exact_from_projection_decimal(value, code)


def test_old_nonterminating_review_digest_requires_fresh_review(history_context, monkeypatch):
    """An old Decimal-based review cannot silently authorize the exact draft."""
    from app.leave_transition_draft import draft_transition, OpeningReleaseDecision
    db, employee, args = setup_source(history_context)
    args['release_evaluated_on'] = date(2026, 2, 28)
    real = recon._exact_from_projection_decimal
    monkeypatch.setattr(recon, '_exact_from_projection_decimal', lambda value, code: Fraction(value))
    legacy = recon.reconcile_selected_opening(db, **args)
    monkeypatch.setattr(recon, '_exact_from_projection_decimal', real)
    current = recon.reconcile_selected_opening(db, **args)
    assert legacy.source_digest == current.source_digest
    assert legacy.reconciliation_digest != current.reconciliation_digest
    openings = tuple(OpeningReleaseDecision(employee.id, code, True, 'Synthetic explicit review')
                     for code in ('A', 'W', 'H'))
    with pytest.raises(ValueError, match='Reviewed reconciliation changed'):
        draft_transition(db, reviewed_reconciliation_digest=legacy.reconciliation_digest,
                         ledger_decisions=(), opening_decisions=openings, **args)
    fresh = draft_transition(db, reviewed_reconciliation_digest=current.reconciliation_digest,
                             ledger_decisions=(), opening_decisions=openings, **args)
    assert fresh.reconciliation_digest == current.reconciliation_digest
    assert not fresh.posting_allowed
