"""Reviewed source-year labels for an existing untagged ledger row, no posting.

Caller owns commit. This records the HR review without rewriting the original
opening, inventing a new balance, or proving source-year completeness.
"""
from datetime import date, datetime, UTC
from hashlib import sha256
import json
import re
from fractions import Fraction

from sqlalchemy import select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_exact_amount import canonical_amount, ledger_amount
from app.leave_year_attribution import plan_year_attribution
from app.models import AdminAccount, AuditLog, CAIRO_TZ, Employee, LeaveBalanceTransaction, LeaveType, LeaveYearAttribution


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def _locks(db, identity):
    if db.get_bind().dialect.name != 'postgresql':
        raise ValueError('Reviewed attribution requires migrated PostgreSQL.')
    actor = _guard(db, **identity)
    db.execute(text('LOCK TABLE audit_logs,employees,leave_balance_transactions,leave_year_attributions IN SHARE ROW EXCLUSIVE MODE'))
    db.execute(text('LOCK TABLE leave_types IN SHARE MODE'))
    return actor


def _source(db, ledger_id):
    row = db.get(LeaveBalanceTransaction, ledger_id, populate_existing=True)
    if row is None or row.earning_year is not None:
        raise ValueError('Existing untagged ledger source required; tagged earning cannot be relabelled.')
    kind = db.get(LeaveType, row.leave_type_id, populate_existing=True)
    employee = db.get(Employee, row.employee_id, populate_existing=True)
    if kind is None or employee is None or not kind.active or not kind.tracks_balance:
        raise ValueError('Existing employee and active balance-tracking leave type required.')
    payload = dict(ledger_id=row.id, employee_id=row.employee_id, leave_type_id=row.leave_type_id,
        type_code=kind.code, active=kind.active, tracks_balance=kind.tracks_balance,
        amount=canonical_amount(ledger_amount(row)), transaction_type=row.transaction_type,
        source_operation_key=row.source_operation_key, source_digest=row.source_digest,
        earning_year=row.earning_year, released_on=row.released_on.isoformat() if row.released_on else None,
        reason=row.reason, created_by=row.created_by, created_at=row.created_at.astimezone(UTC).isoformat())
    return row, payload


def _derive(db, *, ledger_id, allocations, reviewed_on, reason):
    if type(ledger_id) is not int or not 1 <= ledger_id <= 2147483647:
        raise ValueError('Resolved source ledger identity required.')
    if type(reviewed_on) is not date or reviewed_on > datetime.now(CAIRO_TZ).date():
        raise ValueError('Current explicit attribution review date required.')
    row, source = _source(db, ledger_id)
    plan = plan_year_attribution(ledger_id=ledger_id, employee_id=row.employee_id,
        leave_type_id=row.leave_type_id, source_digest=digest(source), source_amount=ledger_amount(row),
        allocations=allocations, reviewed_on=reviewed_on, reason=reason)
    payload = dict(source=source, allocations=[[y, canonical_amount(v)] for y, v in plan.allocations],
        reviewed_on=reviewed_on.isoformat(), reason=plan.reason, attribution_digest=plan.attribution_digest,
        ledger_delta='0/1', proves_year_completeness=False)
    return payload


@bounded_workbook_operation
def review_year_attribution(db, *, ledger_id, allocations, reviewed_on, reason, **identity):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean attribution transaction required.')
    _locks(db, identity)
    payload = _derive(db, ledger_id=ledger_id, allocations=allocations, reviewed_on=reviewed_on, reason=reason)
    return dict(plan=payload, reviewed_digest=digest(payload), posting_allowed=False)


def verify_year_attribution(db, receipt):
    try:
        request, result = json.loads(receipt.request_json), json.loads(receipt.result_json)
        allocations = tuple((y, Fraction(v)) for y, v in result['allocations'])
        recomputed = _derive(db, ledger_id=receipt.ledger_id, allocations=allocations,
            reviewed_on=date.fromisoformat(result['reviewed_on']), reason=result['reason'])
        if recomputed != result:
            raise ValueError('Year attribution plan changed.')
    except (TypeError, KeyError, ValueError, AttributeError, ZeroDivisionError) as exc:
        raise ValueError('Malformed year attribution receipt; refusing, not ignored.') from exc
    if digest(request) != receipt.request_digest or digest(result) != receipt.result_digest:
        raise ValueError('Year attribution receipt integrity changed.')
    # Digests alone are self-consistent for any forged pair; bind the request to THIS result.
    if (not isinstance(request, dict)
            or set(request) != {'reviewed_digest', 'plan', 'actor_id', 'session_id', 'session_version', 'restore_epoch'}
            or request['plan'] != result or request['reviewed_digest'] != receipt.result_digest
            or request['actor_id'] != receipt.actor_id or request['session_id'] != receipt.session_id
            or request['session_version'] != receipt.session_version or request['restore_epoch'] != receipt.restore_epoch):
        raise ValueError('Year attribution request does not bind this result.')
    _, current = _source(db, receipt.ledger_id)
    if (result['source'] != current or digest(current) != receipt.source_digest
            or result['ledger_delta'] != '0/1' or result['proves_year_completeness'] is not False):
        raise ValueError('Attributed source changed; separate reviewed correction required.')
    audits = db.scalars(select(AuditLog).where(AuditLog.action == 'leave_year_attributed',
        AuditLog.entity_type == 'leave_year_attribution', AuditLog.entity_id == receipt.id)).all()
    actor = db.get(AdminAccount, receipt.actor_id)
    if (len(audits) != 1 or audits[0].old_value != receipt.source_digest or audits[0].new_value != receipt.result_json
            or actor is None or audits[0].actor != actor.username or audits[0].reason != result['reason']):
        raise ValueError('Year attribution audit proof changed.')
    return result


@bounded_workbook_operation
def apply_year_attribution(db, *, operation_id, reviewed_digest, human_confirmed,
                           ledger_id, allocations, reviewed_on, reason, **identity):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean attribution transaction required.')
    if (not isinstance(operation_id, str) or re.fullmatch('[0-9a-f]{64}', operation_id) is None
            or not isinstance(reviewed_digest, str) or re.fullmatch('[0-9a-f]{64}', reviewed_digest) is None
            or human_confirmed is not True):
        raise ValueError('Exact reviewed attribution, operation and confirmation required.')
    actor = _locks(db, identity)
    payload = _derive(db, ledger_id=ledger_id, allocations=allocations, reviewed_on=reviewed_on, reason=reason)
    if digest(payload) != reviewed_digest:
        raise ValueError('Attribution source changed; review again.')
    request = dict(reviewed_digest=reviewed_digest, plan=payload, actor_id=actor.id,
        session_id=identity['session_id'], session_version=identity['session_version'],
        restore_epoch=identity['expected_epoch'])
    existing = db.get(LeaveYearAttribution, operation_id, populate_existing=True)
    if existing is not None:
        if existing.request_digest != digest(request):
            raise ValueError('Attribution operation identity changed.')
        return verify_year_attribution(db, existing)
    if db.scalar(select(LeaveYearAttribution.id).where(LeaveYearAttribution.ledger_id == ledger_id)):
        raise ValueError('Ledger source already attributed; separate correction required.')
    with db.begin_nested():
        receipt = LeaveYearAttribution(id=operation_id, ledger_id=ledger_id, actor_id=actor.id,
            session_id=identity['session_id'], session_version=identity['session_version'],
            restore_epoch=identity['expected_epoch'], source_digest=digest(payload['source']),
            request_digest=digest(request), result_digest=digest(payload),
            request_json=canonical(request), result_json=canonical(payload))
        db.add(receipt)
        db.add(AuditLog(actor=actor.username, action='leave_year_attributed', entity_type='leave_year_attribution',
            entity_id=operation_id, old_value=receipt.source_digest, new_value=receipt.result_json, reason=reason.strip()))
        db.flush()
    return payload
