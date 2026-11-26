# ruff: noqa: F811
"""NEGATIVE-SECURITY ORACLES for the B-2171 stored-retry proof replay (independent review).

The B retry branch must verify the STORED raw proofs (_verify_versioned_checkpoint_sources)
instead of trusting digests/audit/request alone. leave_year_transitions is DB-immutable
(0076 trigger blocks UPDATE/DELETE/TRUNCATE) and unique per (employee, source_year), so
each probe below INSERTs a complete fabricated checkpoint row set (row + audit) inside a
SAVEPOINT that is rolled back afterwards: labelled controlled stored-source fabrication
modelling a future writer bug, not a legitimate business flow; no source-bound ledger row
is touched and no immutability guard is weakened (inserts are the app's own write path).
Row/request/audit digests are consistent by construction, so any refusal must come from
the new proof-replay line itself. Probe 0 shows a VALID fabricated checkpoint is accepted
by the retry with no new writes, isolating the refusals to the tamper.
"""
import copy
from datetime import date, datetime

import pytest
from sqlalchemy import func, select

from app.leave_month_earning import canonical, digest
from app.models import AdminAccount, AuditLog, CAIRO_TZ, LeaveBalanceTransaction, LeaveYearTransition
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_net_source import FULL, zero_year, Clock, IDENT
from tests.test_leave_cash_out import payroll


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_stored_retry_tamper_refusals_and_identity_bound(prior_year, monkeypatch):
    from app.leave_zero_decision import record_zero_decision, review_zero_decision
    from app.leave_carry_allocation_store import record_carry_allocation, review_carry_allocation
    from app.leave_annual_settlement_store import apply_annual_settlement, review_annual_settlement_apply
    from app.leave_year_transition import record_year_transition, review_year_transition
    monkeypatch.setattr('app.leave_cash_out.datetime', Clock)
    db, args = zero_year(prior_year, monkeypatch)
    emp = args['employee_id']
    kinds = prior_year[2]
    sources = {c: {**args, 'leave_type_id': kinds[c]} for c in ('A', 'W', 'H')}
    monkeypatch.setattr('app.leave_carry_expiry.datetime', Clock)
    from app.leave_carry_expiry import apply_carry_expiry, review_carry_expiry
    from app.models import LeaveAnnualSettlement
    old_settle = db.scalar(select(LeaveAnnualSettlement).where(LeaveAnnualSettlement.source_year == 2025))
    ex = dict(grant_kind='annual', grant_id=old_settle.id, employee_id=emp, leave_type_id=kinds['A'],
        evaluated_on=date(2027, 1, 2), reason='Synthetic explicit old annual expiry', **{k: args[k] for k in IDENT})
    rx = review_carry_expiry(db, **ex)
    apply_carry_expiry(db, operation_id='8' * 64, reviewed_digest=rx['reviewed_digest'], human_confirmed=True, **ex)
    _run, item = payroll(db, prior_year[1], start=date(2026, 12, 1), end=date(2026, 12, 31), status='draft')
    sel = dict(source_selection=sources['A'], full_annual_entitlement=21, payroll_item_id=item.id,
        available_from=date(2027, 1, 2), available_through=date(2027, 4, 30), reason='Synthetic 2026 annual close')
    rv = review_annual_settlement_apply(db, **sel)
    apply_annual_settlement(db, operation_id='5' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, **sel)
    zr = review_zero_decision(db, source_selection=sources['W'], reason='Synthetic 2026 zero W')
    record_zero_decision(db, operation_id='6' * 64, reviewed_digest=zr['reviewed_digest'], human_confirmed=True,
        source_selection=sources['W'], reason='Synthetic 2026 zero W')
    csel = dict(source_selection=sources['H'], available_from=None, available_through=None, reason='Synthetic 2026 H closing')
    cr = review_carry_allocation(db, **csel)
    record_carry_allocation(db, operation_id='7' * 64, reviewed_digest=cr['reviewed_digest'], human_confirmed=True, **csel)
    call = dict(source_selections=sources, reason='Synthetic 2026 transition')
    plan = review_year_transition(db, **call)['plan']
    actor = db.get(AdminAccount, args['actor_id'])

    def fabricate(stored, stored_request, op_id):
        request = dict(plan=stored, reviewed_digest=digest(stored), actor_username=actor.username,
            **{k: sources['A'][k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')})
        request.update(stored_request)
        row = LeaveYearTransition(id=op_id, employee_id=stored['employee_id'], source_year=stored['source_year'],
            target_year=stored['target_year'], source_digest=digest(stored), request_json=canonical(request),
            request_digest=digest(request), result_json=canonical(stored), result_digest=digest(stored),
            created_at=datetime.now(CAIRO_TZ))
        db.add(row)
        db.add(AuditLog(actor=actor.username, action='leave_year_transition_recorded',
            entity_type='leave_year_transition', entity_id=op_id, old_value=digest(stored),
            new_value=row.result_json, reason=stored['reason']))
        db.flush()
        return digest(stored)

    # Probe 0 (control): a VALID fabricated stored checkpoint replays exactly, no new writes.
    sp0 = db.begin_nested()
    valid_digest = fabricate(plan, {}, '9' * 64)
    before_count = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    import json as _json
    assert record_year_transition(db, operation_id='9' * 64, reviewed_digest=valid_digest,
                                  human_confirmed=True, **call) == _json.loads(canonical(plan))
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == before_count
    sp0.rollback()
    db.expire_all()

    # Probe 1: stored checkpoint carrying a non-v2 versioned source is refused BY THE REPLAY.
    sp1 = db.begin_nested()
    bad = copy.deepcopy(plan)
    bad['balances']['W']['source']['version'] = 'year2-recorded-v1'
    bad_digest = fabricate(bad, {}, 'a' * 64)
    with pytest.raises(ValueError, match='Transition recorded source version is not admitted'):
        record_year_transition(db, operation_id='a' * 64, reviewed_digest=bad_digest, human_confirmed=True, **call)
    sp1.rollback()
    db.expire_all()

    # Probe 2: stored checkpoint with a tampered v2 net is refused BY THE REPLAY.
    sp2 = db.begin_nested()
    bad = copy.deepcopy(plan)
    bad['balances']['H']['source']['net_amount'] = '123/1'
    bad['balances']['H']['source_digest'] = digest(bad['balances']['H']['source'])
    bad_digest = fabricate(bad, {}, 'b' * 64)
    with pytest.raises(ValueError):
        record_year_transition(db, operation_id='b' * 64, reviewed_digest=bad_digest, human_confirmed=True, **call)
    sp2.rollback()
    db.expire_all()

    # Probe 3: stored-request session_version not matching this retry's identity is refused.
    sp3 = db.begin_nested()
    good_digest = fabricate(plan, {'session_version': sources['A']['session_version'] + 1}, 'c' * 64)
    with pytest.raises(ValueError, match='Year-transition recorded evidence changed'):
        record_year_transition(db, operation_id='c' * 64, reviewed_digest=good_digest, human_confirmed=True, **call)
    sp3.rollback()
    db.expire_all()
