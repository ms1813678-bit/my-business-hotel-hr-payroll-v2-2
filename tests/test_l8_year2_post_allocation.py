# ruff: noqa: F811
"""Candidate tests: v2 recorded proof of a year source read AFTER the year's zero (real) or cash (real, year 2025) debit."""
import copy
import json
from fractions import Fraction

import pytest
from sqlalchemy import select

from app.leave_exact_amount import canonical_amount
from app.leave_month_earning import digest
from app.leave_year2_recorded_source import build_year2_recorded_source
from app.leave_year2_proof_guard import MAX_DEPTH, active_stack, guarded
from app.leave_year2_recorded_source_v2 import (build_year2_recorded_source_v2, split_allocations,
    verify_year_proof, verify_year2_recorded_source_v2, _check_allocations)
from app.models import LeaveBalanceTransaction, LeaveCashOut, LeaveAnnualSettlement
from tests.test_l8_year2_net_source import (zero_year, FULL, prior_year, context, history_context)  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa

P = pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)


def seal(stored):
    stored['source_digest'] = digest(stored['source'])
    return stored


def check(db, args, stored):
    return verify_year2_recorded_source_v2(db, stored=stored, employee_id=args['employee_id'], leave_type_id=args['leave_type_id'])


def post_zero_year(prior_year, monkeypatch):
    """Real 2026 W zero decision recorded through the real zero service. The test (not the product) routes the
    later-year source through the isolated adapter; no source file is changed."""
    import app.leave_zero_decision as zero_module
    from app.leave_year2_completed_shape import review_year2_as_completed_year_source
    from app.leave_zero_decision import record_zero_decision, review_zero_decision
    db, args = zero_year(prior_year, monkeypatch)
    pre = build_year2_recorded_source(db, **args)                      # v1 before the zero: the settlement-style pre source
    original = zero_module.review_completed_year_source
    monkeypatch.setattr(zero_module, 'review_completed_year_source',
        lambda db_, **sel: review_year2_as_completed_year_source(db_, **sel) if sel['source_year'] == 2026 else original(db_, **sel))
    review = review_zero_decision(db, source_selection=args, reason='Synthetic year 2 zero')
    record_zero_decision(db, operation_id='9' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True,
        source_selection=args, reason='Synthetic year 2 zero')
    return db, args, pre


@P
def test_control_old_verifier_and_v1_builder_cannot_prove_a_post_zero_source(prior_year, monkeypatch):
    db, args, pre = post_zero_year(prior_year, monkeypatch)
    with pytest.raises(ValueError, match='monthly receipt provenance'):
        build_year2_recorded_source(db, **args)


@P
def test_post_zero_2026_v2_proof_verifies_json_round_trip_held_unchanged_and_survives_later_rows(prior_year, monkeypatch):
    from app.leave_year2_net_source import review_year2_net_source
    db, args, pre = post_zero_year(prior_year, monkeypatch)
    out = build_year2_recorded_source_v2(db, **args)
    stored = json.loads(json.dumps(out['stored']))
    verified = check(db, args, stored)
    proof = stored['source']
    assert verified['net'] == 0 and out['amount'] == 0 and not out['posting_allowed']
    assert len(proof['allocations']) == 1 and proof['allocations'][0]['kind'] == 'zero'
    assert verified['base_net'] > 0 and verified['allocation_amount'] == -verified['base_net']      # exactly once
    assert proof['held_annual'] == pre['stored']['source']['held_annual']                           # held unchanged
    assert verify_year_proof(db, stored=stored, employee_id=args['employee_id'], leave_type_id=args['leave_type_id'])['net'] == 0
    # later legitimate rows: the live source refuses ("unrelated ledger"), the recorded proof still verifies
    db.add(LeaveBalanceTransaction(employee_id=args['employee_id'], leave_type_id=args['leave_type_id'],
        transaction_type='adjustment', days_delta=1, reason='Synthetic later adjustment', created_by='synthetic'))
    db.flush()
    with pytest.raises(ValueError, match='unrelated ledger'):
        review_year2_net_source(db, **args)
    assert check(db, args, stored)['net'] == 0


def _mutations():
    a0 = lambda s: s['source']['allocations'][0]                       # noqa: E731
    return {
        'digest': lambda s: s.__setitem__('source_digest', '0' * 64),
        'version_v1': lambda s: s['source'].__setitem__('version', 'year2-recorded-v1'),
        'extra_key': lambda s: s['source'].__setitem__('x', 1),
        'net_amount': lambda s: s['source'].__setitem__('net_amount', '1/1'),
        'carried_amount': lambda s: s.__setitem__('carried_amount', '1/1'),
        'allocation_total': lambda s: s['source'].__setitem__('allocation_amount', '0/1'),
        'held_annual': lambda s: s['source'].__setitem__('held_annual', '1/1'),
        'alloc_amount': lambda s: a0(s).__setitem__('amount', '-1/1'),
        'alloc_amount_positive': lambda s: a0(s).__setitem__('amount', '1/1'),
        'alloc_ledger_id': lambda s: a0(s).__setitem__('ledger_id', s['source']['base']['source']['own']['source']['selected_ids'][0]),
        'alloc_receipt_id': lambda s: a0(s).__setitem__('receipt_id', 'e' * 64),
        'alloc_kind_cash': lambda s: a0(s).__setitem__('kind', 'cash'),
        'alloc_kind_unknown': lambda s: a0(s).__setitem__('kind', 'manual'),
        'alloc_source_digest': lambda s: a0(s).__setitem__('source_digest', '0' * 64),
        'alloc_result_digest': lambda s: a0(s).__setitem__('result_digest', '0' * 64),
        'alloc_annual_id': lambda s: a0(s).__setitem__('annual_id', 'e' * 64),
        'alloc_extra_key': lambda s: a0(s).__setitem__('x', 1),
        'omitted_allocation': lambda s: s['source']['allocations'].clear(),
        'double_counted_allocation': lambda s: s['source']['allocations'].append(copy.deepcopy(a0(s))),
        'base_swapped_to_v2_label': lambda s: s['source']['base']['source'].__setitem__('version', 'year2-recorded-v2'),
        'base_year': lambda s: s['source']['base'].__setitem__('source_year', 2027),
        'base_amount': lambda s: s['source']['base'].__setitem__('carried_amount', '1/1'),
    }


@P
def test_every_post_allocation_tamper_refuses_even_resealed(prior_year, monkeypatch):
    db, args, _ = post_zero_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source_v2(db, **args)['stored']))
    check(db, args, stored)
    muts = _mutations()
    assert len(muts) >= 20
    for name, mutate in muts.items():
        bad = copy.deepcopy(stored)
        mutate(bad)
        if name != 'digest':
            seal(bad)
        with pytest.raises(ValueError):
            check(db, args, bad)
    check(db, args, stored)
    # resealed base: base digest alone changed with the outer digest resealed still refuses
    bad = copy.deepcopy(stored)
    bad['source']['base']['source']['net_amount'] = '9/1'
    with pytest.raises(ValueError):
        check(db, args, seal(bad))


@P
def test_omitting_the_zero_allocation_or_counting_it_twice_cannot_be_built(prior_year, monkeypatch):
    """Net exactness: base + allocations must equal the live net once. Break each side of the identity directly."""
    db, args, _ = post_zero_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source_v2(db, **args)['stored']))
    omit = copy.deepcopy(stored)
    omit['source']['allocations'] = []
    omit['source']['allocation_amount'] = '0/1'
    with pytest.raises(ValueError, match='exactly once'):          # base net > 0 but the recorded net says 0
        check(db, args, seal(omit))
    omit['source']['net_amount'] = omit['carried_amount'] = omit['source']['base']['carried_amount']
    omit = seal(omit)                                               # now internally consistent but the zero debit is unnamed
    assert check(db, args, omit)['net'] > 0                          # verifies as the PRE-zero view only ...
    from app.leave_year2_net_source import review_year2_net_source
    assert review_year2_net_source(db, **args)['amount'] == 0       # ... which is not the live net: the builder compares and refuses


def _own_shape(rows, selected, amount):
    return dict(source=dict(rows=rows, selected_ids=selected, amount=amount), selection_id='a' * 64)


def test_split_is_exhaustive_pure_and_recomputes_the_pre_allocation_amount():
    rows = [[1, '3/1', 'adjustment', 'month_earning:1:2026-01-01:W:earned', 'a' * 64, 2026, '2026-01-01'],
            [2, '-1/1', 'adjustment', 'leave_cashout:' + 'b' * 64, 'c' * 64, 2026, '2026-12-31'],
            [3, '-1/1', 'adjustment', 'leave_zero:' + 'd' * 64, 'e' * 64, 2026, '2026-12-31'],
            [4, '-1/1', 'adjustment', 'leave_carry_expiry:' + 'f' * 64, 'e' * 64, 2025, '2026-12-31']]
    pre, allocs = split_allocations(_own_shape(rows, [1, 2, 3], '1/1'))
    assert [a['kind'] for a in allocs] == ['cash', 'zero'] and pre['source']['amount'] == '3/1'
    assert pre['source']['selected_ids'] == [1] and [r[0] for r in pre['source']['rows']] == [1, 4]
    for bad_key in ('manual_opening:x', '', 'leave_cashout_x:1'):
        bad = [row[:] for row in rows]
        bad[0][3] = bad_key
        with pytest.raises(ValueError, match='Unknown selected row kind'):
            split_allocations(_own_shape(bad, [1], '3/1'))
    with pytest.raises(ValueError, match='missing'):
        split_allocations(_own_shape(rows, [99], '0/1'))


def test_guard_rank_stack_pure():
    a, b = (1, 2, 2026, 1), (1, 2, 2026, 0)
    assert guarded(a, lambda: guarded(b, lambda: active_stack())) == (a, b)       # v2(Y) -> v1(Y)
    assert active_stack() == ()
    for outer, inner in ((a, a), (b, a), (b, b), ((1, 2, 2025, 0), b), ((1, 2, 2026, 0), (1, 2, 2027, 0))):
        with pytest.raises(ValueError, match='strictly decrease'):
            guarded(outer, lambda inner=inner: guarded(inner, lambda: None))
        assert active_stack() == ()                                                  # finally popped on refusal
    guarded(a, lambda: [guarded((1, 2, 2025, 0), lambda: None), guarded((1, 2, 2025, 0), lambda: None)])  # siblings ok
    with pytest.raises(ValueError, match='well formed'):
        guarded((1, 2, '2026', 0), lambda: None)
    def deep(n):
        return guarded((1, 2, 2026 - n, 0), lambda: deep(n + 1))
    with pytest.raises(ValueError, match='resource budget'):
        deep(0)
    assert active_stack() == () and MAX_DEPTH == 8


def test_guard_context_is_isolated_per_thread():
    import threading
    seen = []
    def run():
        seen.append(active_stack())
    guarded((1, 2, 2026, 1), lambda: (lambda t: (t.start(), t.join()))(threading.Thread(target=run)))
    assert seen == [()]


@P
def test_guard_reaches_unmodified_legacy_frame_via_the_real_router(prior_year, monkeypatch):
    """The real v2 verifier calls verify_zero_ledger_for_year (an unmodified legacy frame, no context argument). Replace
    it with a frame that re-enters the REAL router: same-year v2 and v2->v2 must refuse, an unrelated earlier year may pass
    the guard. The stack is empty again afterwards."""
    import app.leave_zero_decision as zero_module
    from app.leave_recorded_source_versions import verify_versioned_recorded_source
    db, args, _ = post_zero_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source_v2(db, **args)['stored']))
    e, t = args['employee_id'], args['leave_type_id']
    result = dict(stored, held_annual=stored['source']['held_annual'])
    assert verify_versioned_recorded_source(db, result=result, employee_id=e, leave_type_id=t)   # top-level router entry
    assert active_stack() == ()
    real = zero_module.verify_zero_ledger_for_year
    seen = []
    def reenter(db_, ledger, **kw):
        assert len(active_stack()) == 1 and active_stack()[0][2:] == (2026, 1)   # router frame is visible here
        for attempt in (result, dict(result, source=dict(stored['source']['base']['source']),
                        source_digest=stored['source']['base']['source_digest'],
                        carried_amount=stored['source']['base']['carried_amount'])):
            try:
                verify_versioned_recorded_source(db_, result=dict(attempt, held_annual=attempt['source']['held_annual']),
                                                 employee_id=e, leave_type_id=t)
                seen.append('accepted')
            except ValueError as exc:
                seen.append(str(exc))
        return real(db_, ledger, **kw)
    monkeypatch.setattr(zero_module, 'verify_zero_ledger_for_year', reenter)
    verify_versioned_recorded_source(db, result=result, employee_id=e, leave_type_id=t)
    assert seen[0].startswith('Proof dependency must strictly decrease')      # v2(2026) re-enters v2(2026)
    assert seen[1] == 'accepted'                                               # v1(2026) under v2(2026): lower rank passes the guard
    assert active_stack() == ()


def _rows_of(stored):
    return stored['source']['base']['source']['own']['source']


@P
def test_partition_tamper_cases_refuse_even_resealed(prior_year, monkeypatch):
    db, args, _ = post_zero_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source_v2(db, **args)['stored']))
    check(db, args, stored)
    alloc = stored['source']['allocations'][0]
    usage = stored['source']['base']['source']['consumptions']
    def same_in_base_rows(s):
        _rows_of(s)['rows'].append([alloc['ledger_id'], alloc['amount'], 'adjustment', 'leave_zero:' + alloc['receipt_id'], 'e' * 64, 2026, '2026-12-31'])
    def same_in_base_selected(s):
        _rows_of(s)['selected_ids'].append(alloc['ledger_id'])
    def dup_alloc(s):
        s['source']['allocations'].append(dict(alloc))
    def usage_equals_alloc(s):
        s['source']['base']['source']['consumptions'].append(dict(usage[0] if usage else {}, usage_ledger_id=alloc['ledger_id']))
    def base_year_cash_row(s):
        _rows_of(s)['rows'].append([987654, '-1/1', 'adjustment', 'leave_cashout:' + 'a' * 64, 'e' * 64, 2026, '2026-12-31'])
    def base_year_zero_row(s):
        _rows_of(s)['rows'].append([987655, '-1/1', 'adjustment', 'leave_zero:' + 'a' * 64, 'e' * 64, 2026, '2026-12-31'])
    for name, mutate in dict(rows=same_in_base_rows, selected=same_in_base_selected, dup=dup_alloc,
            usage=usage_equals_alloc, cash_row=base_year_cash_row, zero_row=base_year_zero_row).items():
        bad = copy.deepcopy(stored)
        mutate(bad)
        bad['source']['base']['source_digest'] = digest(bad['source']['base']['source'])   # reseal base AND outer
        seal(bad)
        with pytest.raises(ValueError):
            check(db, args, bad)
    # other-year cash rows in the full history list stay legal: verified unchanged proof still passes
    check(db, args, stored)


# ---- real annual CASH allocation (year 2025: the only real settled cash in the existing fixtures) -----------------
from tests.test_l8_source_year_cash_binding import settle                # noqa: E402


def cash_binding(db, employee_id, leave_type_id):
    cash = db.scalar(select(LeaveCashOut).where(LeaveCashOut.employee_id == employee_id, LeaveCashOut.leave_type_id == leave_type_id))
    annual = db.get(LeaveAnnualSettlement, cash.id)
    ledger = db.get(LeaveBalanceTransaction, cash.ledger_id)
    from app.leave_exact_amount import ledger_amount
    return dict(kind='cash', ledger_id=ledger.id, receipt_id=cash.id, amount=canonical_amount(ledger_amount(ledger)),
        annual_id=cash.id, source_digest=annual.source_digest, result_digest=annual.result_digest)


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_real_annual_cash_allocation_verifies_with_the_live_functions_and_every_detachment_refuses(prior_year, monkeypatch):
    db, employee, kinds, item, auth = settle(prior_year)
    e, t = employee.id, kinds['A']
    good = cash_binding(db, e, t)
    assert Fraction(good['amount']) < 0
    assert _check_allocations(db, [good], employee_id=e, leave_type_id=t, year=2025) == Fraction(good['amount'])
    mutations = {
        'year': lambda b: None,                                        # wrong year handled below
        'amount': lambda b: b.__setitem__('amount', '-1/1'),
        'amount_positive': lambda b: b.__setitem__('amount', canonical_amount(-Fraction(b['amount']))),
        'ledger_id': lambda b: b.__setitem__('ledger_id', b['ledger_id'] + 1),
        'receipt_id': lambda b: b.__setitem__('receipt_id', 'e' * 64),
        'annual_id': lambda b: b.__setitem__('annual_id', None),
        'kind_zero': lambda b: b.__setitem__('kind', 'zero'),
        'source_digest': lambda b: b.__setitem__('source_digest', '0' * 64),
        'result_digest': lambda b: b.__setitem__('result_digest', '0' * 64),
    }
    for name, mutate in mutations.items():
        if name == 'year':
            continue
        bad = copy.deepcopy(good)
        mutate(bad)
        with pytest.raises(ValueError):
            _check_allocations(db, [bad], employee_id=e, leave_type_id=t, year=2025)
    with pytest.raises(ValueError):                                    # same receipt claimed for another year
        _check_allocations(db, [good], employee_id=e, leave_type_id=t, year=2026)
    with pytest.raises(ValueError):                                    # another employee / type
        _check_allocations(db, [good], employee_id=e + 1, leave_type_id=t, year=2025)
    with pytest.raises(ValueError):
        _check_allocations(db, [good], employee_id=e, leave_type_id=kinds['W'], year=2025)
    with pytest.raises(ValueError, match='binding changed or duplicated'):
        _check_allocations(db, [good, copy.deepcopy(good)], employee_id=e, leave_type_id=t, year=2025)


@P
def test_builder_refuses_a_split_that_omits_or_double_counts_the_zero_debit(prior_year, monkeypatch):
    import app.leave_year2_recorded_source_v2 as v2
    db, args, _ = post_zero_year(prior_year, monkeypatch)
    real = v2.split_allocations
    monkeypatch.setattr(v2, 'split_allocations', lambda own: (real(own)[0], []))                  # omission
    with pytest.raises(ValueError, match='omits or double counts'):
        build_year2_recorded_source_v2(db, **args)
    monkeypatch.setattr(v2, 'split_allocations', lambda own: (real(own)[0], real(own)[1] * 2))    # double count
    with pytest.raises(ValueError, match='omits or double counts'):
        build_year2_recorded_source_v2(db, **args)
    monkeypatch.setattr(v2, 'split_allocations', real)
    assert build_year2_recorded_source_v2(db, **args)['amount'] == 0
