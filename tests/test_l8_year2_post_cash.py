# ruff: noqa: F811
"""Full 2026 post-cash proof: the real guarded annual settlement writes a real 2026 cash debit, then v2 is built and
verified through the real versioned router. py3.10 shim, fixed clock 2027, read-only source amount, not a balance."""
import copy
import json
from datetime import date
from fractions import Fraction

import pytest
from sqlalchemy import select

from app.leave_annual_settlement_store import apply_annual_settlement, review_annual_settlement_apply
from app.leave_month_earning import digest
from app.leave_year2_proof_guard import active_stack
from app.leave_year2_recorded_source_v2 import VERSION, build_year2_recorded_source_v2, verify_year_proof
from app.leave_recorded_source_versions import verify_versioned_recorded_source
from app.models import LeaveAnnualSettlement, LeaveBalanceTransaction
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_recorded_source import annual_year
from tests.test_l8_year2_net_source import FULL, Clock
from tests.test_leave_cash_out import payroll


def settled_2026(prior_year, monkeypatch):
    monkeypatch.setattr('app.leave_cash_out.datetime', Clock)
    db, args = annual_year(prior_year, monkeypatch)
    monkeypatch.setattr('app.leave_carry_expiry.datetime', Clock)
    from app.leave_carry_expiry import review_carry_expiry, apply_carry_expiry
    old = db.scalar(select(LeaveAnnualSettlement).where(LeaveAnnualSettlement.source_year == 2025))
    expiry = dict(grant_kind='annual', grant_id=old.id, employee_id=args['employee_id'],
        leave_type_id=args['leave_type_id'], evaluated_on=date(2027, 1, 2), reason='Synthetic explicit old annual expiry',
        **{k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')})
    reviewed = review_carry_expiry(db, **expiry)
    apply_carry_expiry(db, operation_id='8' * 64, reviewed_digest=reviewed['reviewed_digest'], human_confirmed=True, **expiry)
    _run, item = payroll(db, prior_year[1], start=date(2026, 12, 1), end=date(2026, 12, 31), status='draft')
    selection = dict(source_selection=args, full_annual_entitlement=21, payroll_item_id=item.id,
        available_from=date(2027, 1, 2), available_through=date(2027, 4, 30), reason='Synthetic year2 annual settlement')
    review = review_annual_settlement_apply(db, **selection)
    apply_annual_settlement(db, operation_id='9' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **selection)
    return db, args


def via_router(db, args, stored):
    result = dict(stored, held_annual=stored['source']['held_annual'])
    return verify_versioned_recorded_source(db, result=result, employee_id=args['employee_id'], leave_type_id=args['leave_type_id'])


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_real_2026_post_cash_v2_builds_verifies_and_refuses_partition_and_reentry(prior_year, monkeypatch):
    db, args = settled_2026(prior_year, monkeypatch)
    e, t = args['employee_id'], args['leave_type_id']
    cash_row = db.scalar(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id == e,
        LeaveBalanceTransaction.earning_year == 2026, LeaveBalanceTransaction.source_operation_key.like('leave_cashout:%')))
    assert cash_row is not None and cash_row.days_delta < 0                   # real 2026 cash debit, via the real settlement
    out = build_year2_recorded_source_v2(db, **args)
    stored = json.loads(json.dumps(out['stored']))
    proof = stored['source']
    assert proof['version'] == VERSION and [a['kind'] for a in proof['allocations']] == ['cash']
    assert proof['allocations'][0]['ledger_id'] == cash_row.id
    assert out['posting_allowed'] is False and active_stack() == ()
    # net = base + the named cash debit exactly once
    assert Fraction(proof['net_amount']) == Fraction(proof['base']['carried_amount']) + Fraction(proof['allocation_amount'])
    # direct entry and the real router both verify; the router's frame runs the real annual verifier chain (guarded)
    assert verify_year_proof(db, stored=stored, employee_id=e, leave_type_id=t)['net'] == Fraction(proof['net_amount'])
    assert via_router(db, args, stored)
    assert active_stack() == ()
    # partition counterexamples, resealed
    def reseal(mutate):
        bad = copy.deepcopy(stored)
        mutate(bad)
        bad['source']['base']['source_digest'] = digest(bad['source']['base']['source'])
        bad['source_digest'] = digest(bad['source'])
        return bad
    alloc = proof['allocations'][0]
    def in_selected(s):
        s['source']['base']['source']['own']['source']['selected_ids'].append(alloc['ledger_id'])
    def in_rows(s):
        s['source']['base']['source']['own']['source']['rows'].append(
            [alloc['ledger_id'], alloc['amount'], 'adjustment', 'leave_cashout:' + alloc['receipt_id'], 'e' * 64, 2026, '2026-12-31'])
    def twice(s):
        s['source']['allocations'].append(dict(alloc))
    for mutate in (in_selected, in_rows, twice):
        with pytest.raises(ValueError):
            verify_year_proof(db, stored=reseal(mutate), employee_id=e, leave_type_id=t)
        assert active_stack() == ()
    # re-entry: the real cash settlement's stored source is v1(2026) pre-cash (rank lower). Prove that, then show a
    # same-year v2 under the settlement node is refused by the active stack.
    annual = db.get(LeaveAnnualSettlement, alloc['receipt_id'])
    stored_source = json.loads(annual.result_json)['plan']['annual']['source']
    assert stored_source['version'] == 'year2-recorded-v1' and stored_source['source_year'] == 2026
    import app.leave_annual_settlement_store as store
    original = store._verify_plan_rule
    seen = []
    def inject(db_, ann, row):
        try:
            via_router(db_, args, stored)
            seen.append('accepted')
        except ValueError as exc:
            seen.append(str(exc))
        return original(db_, ann, row)
    monkeypatch.setattr(store, '_verify_plan_rule', inject)
    verify_year_proof(db, stored=stored, employee_id=e, leave_type_id=t)
    assert seen and all(m.startswith('Proof dependency must strictly decrease') for m in seen)
    monkeypatch.setattr(store, '_verify_plan_rule', original)
    assert active_stack() == ()
    verify_year_proof(db, stored=stored, employee_id=e, leave_type_id=t)


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_real_nested_chain_depth_and_rank_order_and_malformed_shapes(prior_year, monkeypatch):
    """Records every real guard push while the real router verifies the real 2026 post-cash v2. Every observed push is
    strictly lower than its parent, depth > 2 shows a legitimate multi-level real chain (2026 v2 -> 2026 v1 base -> earlier
    year grant sources). Then malformed partition shapes raise ValueError (not KeyError/TypeError) through the router."""
    import app.leave_year2_proof_guard as guard
    import app.leave_year2_recorded_source_v2 as v2mod
    db, args = settled_2026(prior_year, monkeypatch)
    e, t = args['employee_id'], args['leave_type_id']
    stored = json.loads(json.dumps(build_year2_recorded_source_v2(db, **args)['stored']))
    pushes = []
    real = guard.guarded
    def record(node, fn):
        pushes.append((guard.active_stack(), node))
        return real(node, fn)
    monkeypatch.setattr(guard, 'guarded', record)
    monkeypatch.setattr(v2mod, 'guarded', record)
    assert via_router(db, args, stored)
    assert pushes and max(len(parent) for parent, _ in pushes) >= 1
    for parent, node in pushes:
        assert not parent or (node[2], node[3]) < (parent[-1][2], parent[-1][3])
    print('REAL PUSH CHAIN', [(len(p), n[2:]) for p, n in pushes])
    monkeypatch.undo()
    base = stored['source']['base']['source']['own']['source']
    def reseal(mutate):
        bad = copy.deepcopy(stored)
        mutate(bad)
        bad['source']['base']['source_digest'] = digest(bad['source']['base']['source'])
        bad['source_digest'] = digest(bad['source'])
        return bad
    shapes = [lambda s: s['source']['allocations'].__setitem__(0, 7),
              lambda s: s['source']['allocations'][0].pop('ledger_id'),
              lambda s: s['source']['allocations'].__setitem__(0, {'ledger_id': [], 'receipt_id': {}}),
              lambda s: s['source']['base']['source']['own']['source']['rows'].__setitem__(0, [1]),
              lambda s: s['source']['base']['source']['own']['source']['rows'].__setitem__(0, 'x'),
              lambda s: s['source']['base']['source'].__setitem__('consumptions', [{}]),
              lambda s: s['source']['base']['source']['own']['source'].pop('selected_ids'),
              lambda s: s['source'].__setitem__('allocations', None)]
    assert base['rows']
    for mutate in shapes:
        with pytest.raises(ValueError):
            via_router(db, args, reseal(mutate))
        with pytest.raises(ValueError):
            verify_year_proof(db, stored=reseal(mutate), employee_id=e, leave_type_id=t)
        assert active_stack() == ()
