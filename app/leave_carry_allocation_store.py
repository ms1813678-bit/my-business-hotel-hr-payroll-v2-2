"""Reviewed carry allocation receipts, no duplicate credit or spend consumer.

This records a year-source allocation only. No cash, zero/expiry, positive annual
policy, automatic deduction or grant availability consumer is implemented here.
Caller owns commit. An allocation does not yet authorize operational spending.
"""
from datetime import date
import json
import re

from sqlalchemy import select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_completed_year_source import review_completed_year_source
from app.leave_month_earning import _locks, canonical, digest
from app.leave_exact_amount import canonical_amount
from app.models import AuditLog, LeaveCarryAllocation


def _derive(db, *, available_from, available_through, reason, source_selection):
    source = review_completed_year_source(db, **source_selection)
    kind = source['source']['source']['leave_type_code']
    amount = source['amount']
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError('Explicit bounded carry reason required.')
    from app.models import LeaveZeroDecision
    if db.scalar(select(LeaveZeroDecision.id).where(LeaveZeroDecision.employee_id == source_selection['employee_id'],
            LeaveZeroDecision.leave_type_id == source_selection['leave_type_id'],
            LeaveZeroDecision.source_year == source_selection['source_year'])):
        raise ValueError('Source year already zeroed; carry and zero are exclusive.')
    if kind == 'annual' and amount > 0:
        raise ValueError('Positive annual uses the separate source-bound cash and carry settlement service.')
    if amount > 0:
        if (type(available_from) is not date or type(available_through) is not date
                or not source_selection['reviewed_on'] <= available_from <= available_through):
            raise ValueError('Positive carry requires explicit grant-time availability dates.')
    elif available_from is not None or available_through is not None:
        raise ValueError('Debt or zero carry has no positive availability grant.')
    return dict(employee_id=source_selection['employee_id'], leave_type_id=source_selection['leave_type_id'],
        source_year=source_selection['source_year'], target_year=source_selection['source_year'] + 1,
        source_digest=source['source_digest'], source=source['source'],
        carried_amount=canonical_amount(amount), ledger_delta='0/1', cash_amount='0/1',
        held_annual=canonical_amount(source['held_annual']),
        available_from=available_from.isoformat() if available_from else None,
        available_through=available_through.isoformat() if available_through else None,
        reason=reason.strip(), operational_spending_allowed=False)


def _selection_proof(source):
    """Canonical exact request inputs, including the explicit tier evidence maps."""
    normalized = {k: v.isoformat() if isinstance(v, date) else v for k, v in source.items()
        if k not in ('current_insurance_years', 'age_evaluation_dates')}
    normalized['insurance'] = sorted([i, m.isoformat(), None if v is None else str(v)]
        for (i, m), v in source['current_insurance_years'].items())
    normalized['age_dates'] = sorted([m.isoformat(), v.isoformat()]
        for m, v in source['age_evaluation_dates'].items())
    return normalized


def _allocation_locks(db):
    db.execute(text('LOCK TABLE leave_carry_allocations,leave_zero_decisions IN SHARE ROW EXCLUSIVE MODE'))


@bounded_workbook_operation
def review_carry_allocation(db, *, source_selection, available_from=None, available_through=None, reason):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean carry allocation transaction required.')
    payload = _derive(db, source_selection=source_selection, available_from=available_from,
        available_through=available_through, reason=reason)
    return dict(plan=payload, reviewed_digest=digest(payload), posting_allowed=False)


def _verify(db, row, payload, request):
    if (canonical(request.get('plan')) != canonical(payload)
            or request.get('reviewed_digest') != digest(payload)
            or row.employee_id != payload['employee_id'] or row.leave_type_id != payload['leave_type_id']
            or row.source_year != payload['source_year'] or row.source_digest != payload['source_digest']
            or row.request_json != canonical(request) or row.request_digest != digest(request)
            or row.result_json != canonical(payload) or row.result_digest != digest(payload)):
        raise ValueError('Carry allocation identity or evidence changed.')
    audits = db.scalars(select(AuditLog).where(AuditLog.action == 'leave_carry_allocated',
        AuditLog.entity_type == 'leave_carry_allocation', AuditLog.entity_id == row.id)).all()
    if (len(audits) != 1 or audits[0].actor != request['actor_username']
            or audits[0].old_value != row.source_digest or audits[0].new_value != row.result_json
            or audits[0].reason != payload['reason']):
        raise ValueError('Carry allocation audit evidence changed.')
    return payload


@bounded_workbook_operation
def record_carry_allocation(db, *, operation_id, reviewed_digest, human_confirmed, source_selection,
        available_from=None, available_through=None, reason):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean carry allocation transaction required.')
    if (not isinstance(operation_id, str) or re.fullmatch('[0-9a-f]{64}', operation_id) is None
            or not isinstance(reviewed_digest, str) or re.fullmatch('[0-9a-f]{64}', reviewed_digest) is None
            or human_confirmed is not True):
        raise ValueError('Explicit confirmed reviewed carry operation required.')
    identity = {k: source_selection[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    actor = _locks(db, identity)
    _allocation_locks(db)
    previous = db.get(LeaveCarryAllocation, operation_id, populate_existing=True)
    selection_proof = _selection_proof(source_selection)
    if previous is not None:
        request = json.loads(previous.request_json)
        payload = json.loads(previous.result_json)
        # Legacy receipts lack exact input maps. Keep their old strict path.
        if 'source_selection' in request:
            if (request['reviewed_digest'] != reviewed_digest
                    or request['actor_id'] != actor.id or request['actor_username'] != actor.username
                    or request['session_id'] != identity['session_id']
                    or request['session_version'] != identity['session_version']
                    or request['restore_epoch'] != identity['expected_epoch']
                    or request['source_selection'] != selection_proof
                    or payload['available_from'] != (available_from.isoformat() if available_from else None)
                    or payload['available_through'] != (available_through.isoformat() if available_through else None)
                    or payload['reason'] != reason.strip()):
                raise ValueError('Carry allocation identity or evidence changed.')
            from app.leave_carry_availability import _verify_recorded_source, verify_allocation_evidence
            audits = db.scalars(select(AuditLog).where(AuditLog.action == 'leave_carry_allocated',
                AuditLog.entity_type == 'leave_carry_allocation', AuditLog.entity_id == previous.id)).all()
            verify_allocation_evidence(row_id=previous.id, row_employee_id=previous.employee_id,
                row_leave_type_id=previous.leave_type_id, row_source_year=previous.source_year,
                row_request_digest=previous.request_digest, row_source_digest=previous.source_digest,
                row_result_digest=previous.result_digest, row_request_json=previous.request_json,
                row_result_json=previous.result_json,
                audits=[(a.actor, a.old_value, a.new_value, a.reason) for a in audits],
                original_actor_username=actor.username)
            _verify(db, previous, payload, request)
            _verify_recorded_source(db, result=payload, employee_id=previous.employee_id,
                leave_type_id=previous.leave_type_id)
            return payload
    payload = _derive(db, source_selection=source_selection, available_from=available_from,
        available_through=available_through, reason=reason)
    if digest(payload) != reviewed_digest:
        raise ValueError('Carry source changed; review again.')
    request = dict(plan=payload, reviewed_digest=reviewed_digest, actor_id=actor.id,
        actor_username=actor.username, session_id=identity['session_id'],
        session_version=identity['session_version'], restore_epoch=identity['expected_epoch'])
    if previous is None:
        request['source_selection'] = selection_proof
    previous = db.get(LeaveCarryAllocation, operation_id, populate_existing=True)
    if previous is not None:
        return _verify(db, previous, payload, request)
    if db.scalar(select(LeaveCarryAllocation.id).where(LeaveCarryAllocation.employee_id == payload['employee_id'],
            LeaveCarryAllocation.leave_type_id == payload['leave_type_id'],
            LeaveCarryAllocation.source_year == payload['source_year'])):
        raise ValueError('Source year already allocated; separate correction required.')
    with db.begin_nested():
        row = LeaveCarryAllocation(id=operation_id, employee_id=payload['employee_id'],
            leave_type_id=payload['leave_type_id'], source_year=payload['source_year'],
            request_digest=digest(request), source_digest=payload['source_digest'], result_digest=digest(payload),
            request_json=canonical(request), result_json=canonical(payload))
        db.add(row)
        db.add(AuditLog(actor=actor.username, action='leave_carry_allocated', entity_type='leave_carry_allocation',
            entity_id=operation_id, old_value=row.source_digest, new_value=row.result_json, reason=payload['reason']))
        db.flush()
    return json.loads(canonical(_verify(db, row, payload, request)))
