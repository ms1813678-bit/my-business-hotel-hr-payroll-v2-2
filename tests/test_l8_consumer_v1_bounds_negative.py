# ruff: noqa: F811
"""NEGATIVE-SECURITY ORACLES for the three explicit v1-only consumer bounds (batch A 2167).

No producer ever stores a v2 recorded source into a zero decision or a carry allocation
(batch B stores v2 only in year-transition checkpoints), so these bounds are unreachable
through legitimate flows. Each test therefore performs a CONTROLLED STORED-SOURCE
FABRICATION in this disposable test database: a REAL read-only v2 proof is built through
the real builder, then a complete receipt row set (receipt + ledger + audit) is INSERTED
with the v2 source inside and the whole digest/request/ledger/audit evidence chain kept
consistent by construction. Inserts are the app's own normal write path (the
source-immutability trigger only blocks UPDATE/DELETE), so no protection is weakened.
This is a labelled attack fixture proving the refusal fires; it is not a producer claim
and proves nothing about acceptance. The control in each test shows the versioned router
ACCEPTS the same real v2 payload, so the refusal comes from the consumer bound, not from
a broken proof.
"""
import json
from datetime import date
from fractions import Fraction

import pytest

from app.leave_carry_allocation_store import _selection_proof, record_carry_allocation, review_carry_allocation
from app.leave_carry_availability import read_carry_availability
from app.leave_exact_amount import storage_amount
from app.leave_month_earning import canonical, digest
from app.leave_recorded_source_versions import verify_versioned_recorded_source
from app.leave_year2_recorded_source_v2 import build_year2_recorded_source_v2
from app.leave_zero_decision import _verify, review_zero_decision
from app.models import AdminAccount, AuditLog, LeaveBalanceTransaction, LeaveCarryAllocation, LeaveZeroDecision
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_net_source import FULL, full_year

MATCH_CARRY = 'Carry consumer recorded source version is not admitted'
MATCH_ZERO = 'Zero consumer recorded source version is not admitted'


def _tampered(plan, v2):
    result = dict(plan)
    result['source'], result['source_digest'] = v2['source'], v2['source_digest']
    return result


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_zero_consumer_refuses_a_real_v2_stored_source_at_the_boundary(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    actor = db.get(AdminAccount, args['actor_id'])
    # Real read-only v2 proof for the same employee/type/year (plain closed-year ledger).
    v2 = json.loads(json.dumps(build_year2_recorded_source_v2(db, **args)['stored']))
    # Control: the versioned router ACCEPTS this real v2 proof.
    verify_versioned_recorded_source(db, result=dict(v2, held_annual=v2['source']['held_annual']),
                                     employee_id=args['employee_id'], leave_type_id=args['leave_type_id'])
    plan = review_zero_decision(db, source_selection=args, reason='Synthetic year2 closing')['plan']
    result = _tampered(plan, v2)
    request = dict(plan=result, reviewed_digest=digest(result), actor_id=actor.id,
                   actor_username=actor.username, session_id=args['session_id'],
                   session_version=args['session_version'], restore_epoch=args['expected_epoch'])
    # Controlled stored-source fabrication via the app's own insert path.
    op_id = '7' * 64
    ledger = LeaveBalanceTransaction(employee_id=result['employee_id'], leave_type_id=result['leave_type_id'],
        transaction_type='adjustment', **storage_amount(-Fraction(result['zeroed_amount'])),
        reason=result['reason'], created_by=actor.username, source_operation_key=f'leave_zero:{op_id}',
        source_digest=result['source_digest'], earning_year=result['source_year'],
        released_on=args['reviewed_on'])
    db.add(ledger)
    db.flush()
    row = LeaveZeroDecision(id=op_id, employee_id=result['employee_id'], leave_type_id=result['leave_type_id'],
        source_year=result['source_year'], ledger_id=ledger.id, request_digest=digest(request),
        source_digest=result['source_digest'], result_digest=digest(result),
        request_json=canonical(request), result_json=canonical(result))
    db.add(row)
    db.add(AuditLog(actor=actor.username, action='leave_zero_decided', entity_type='leave_zero_decision',
                    entity_id=op_id, old_value=result['source_digest'], new_value=row.result_json,
                    reason=result['reason']))
    db.flush()
    with pytest.raises(ValueError, match=MATCH_ZERO):
        _verify(db, row)


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_carry_consumer_refuses_a_real_v2_stored_source_at_availability_and_stored_retry(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    actor = db.get(AdminAccount, args['actor_id'])
    call = dict(source_selection=args, available_from=date(2027, 1, 2), available_through=date(2027, 4, 30),
                reason='Synthetic year2 closing')
    # Real read-only v2 proof for the same employee/type/year (plain closed-year ledger).
    v2 = json.loads(json.dumps(build_year2_recorded_source_v2(db, **args)['stored']))
    plan = review_carry_allocation(db, **call)['plan']
    result = _tampered(plan, v2)
    request = dict(plan=result, reviewed_digest=digest(result), actor_id=actor.id,
                   actor_username=actor.username, session_id=args['session_id'],
                   session_version=args['session_version'], restore_epoch=args['expected_epoch'],
                   source_selection=_selection_proof(args))
    op_id = '7' * 64
    row = LeaveCarryAllocation(id=op_id, employee_id=result['employee_id'], leave_type_id=result['leave_type_id'],
        source_year=result['source_year'], request_digest=digest(request), source_digest=result['source_digest'],
        result_digest=digest(result), request_json=canonical(request), result_json=canonical(result))
    db.add(row)
    db.add(AuditLog(actor=actor.username, action='leave_carry_allocated', entity_type='leave_carry_allocation',
                    entity_id=op_id, old_value=result['source_digest'], new_value=row.result_json,
                    reason=result['reason']))
    db.flush()
    # Control: the versioned router ACCEPTS this exact fabricated payload (real v2, consistent net).
    verify_versioned_recorded_source(db, result=result, employee_id=args['employee_id'],
                                     leave_type_id=args['leave_type_id'])
    ident = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    # Bound 1: availability read refuses the fabricated v2-sourced allocation.
    with pytest.raises(ValueError, match=MATCH_CARRY):
        read_carry_availability(db, allocation_id=op_id, employee_id=args['employee_id'],
                                leave_type_id=args['leave_type_id'], work_date=date(2027, 1, 3), **ident)
    # Bound 3: stored retry refuses the same fabricated receipt.
    with pytest.raises(ValueError, match=MATCH_CARRY):
        record_carry_allocation(db, operation_id=op_id, reviewed_digest=digest(result),
                                human_confirmed=True, **call)
