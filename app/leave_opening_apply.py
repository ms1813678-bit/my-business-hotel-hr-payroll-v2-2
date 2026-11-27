"""Isolated apply kernel: confirmed manual opening source to the legacy ledger.

Scope: one employee, exactly three categories (A, W, H), all or nothing. It posts
only transaction_type 'opening' rows through the existing ledger shape and writes
audit rows. It never infers that existing ledger, leave or payroll evidence equals the
entered source: any such evidence refuses. No grant, carry, cash, expiry, close or
settlement behavior. Not wired to any route. Caller owns the transaction.

The A/W/H to leave type mapping is supplied by the caller as {'A': id, 'W': id, 'H': id}
and is part of the reviewed digest. A type that is inactive or does not track balance refuses.
The canonical mapping and making weekly_rest/official_holiday track balance are owned by the
integrating writer, not by this module.

Receipt: each nonzero ledger row carries the source identity (operation key
manual_opening:<selection>:<code> and the apply digest) with earning_year and released_on
NULL. That row shape needs the writer's separate constraint extension; the current 0045 check
requires all four source fields or none. Rows are then immutable under 0046. A per-apply audit
receipt names the selection, digest and ledger ids, and lists zero categories (the ledger
forbids zero, so those have no ledger row).
"""
import json
from datetime import date
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import func, select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_balance_rules import preview_manual_balance, validate_manual_balance_change
from app.leave_opening_source import read_confirmed_opening_source
from app.models import (
    AuditLog,
    Employee,
    LeaveBalanceTransaction,
    LeaveRecord,
    LeaveType,
)

CODES = ('A', 'W', 'H')
CANONICAL_TYPES = {'A': 'annual', 'W': 'weekly_rest', 'H': 'official_holiday'}
RECEIPT_ACTION = 'leave_opening_applied'


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _type_map(type_map):
    if (type(type_map) is not dict or set(type_map) != set(CODES)
            or any(type(v) is not int or isinstance(v, bool) or v < 1 for v in type_map.values())
            or len(set(type_map.values())) != 3):
        raise ValueError('Explicit distinct leave type ids for A, W and H required.')
    return {code: type_map[code] for code in CODES}


def _barrier(db, basis):
    """Same basis-month barrier as reconciliation approval, no new period rule."""
    from app.leave_reconciliation_approval import _barrier as approval_barrier
    approval_barrier(db, basis.replace(day=1))


def _receipt_body(raw):
    try:
        body = json.loads(raw)
        if (not isinstance(body, dict) or type(body.get('employee_id')) is not int
                or not isinstance(body.get('type_ids'), dict)
                or not isinstance(body.get('amounts'), dict)
                or set(body['amounts']) != set(CODES)
                or not isinstance(body.get('ledger_ids'), list)
                or not isinstance(body.get('apply_sha256'), str)):
            raise ValueError('Malformed opening receipt evidence.')
        _type_map(body['type_ids'])
        if any(type(i) is not int for i in body['ledger_ids']):
            raise ValueError('Malformed opening receipt evidence.')
        return body
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError('Malformed opening receipt evidence.') from exc


def _evidence(db, *, employee_id, type_map):
    """Current state this apply depends on. Raises on any conflicting evidence."""
    with db.no_autoflush:
        employee = db.execute(select(Employee.id, Employee.code, Employee.status).where(
            Employee.id == employee_id)).first()
        if employee is None or employee.status != 'active':
            raise ValueError('Current active employee required.')
        types = {}
        for code, type_id in type_map.items():
            row = db.execute(select(LeaveType.id, LeaveType.code, LeaveType.active,
                LeaveType.tracks_balance).where(LeaveType.id == type_id)).first()
            if row is None or not row.active or not row.tracks_balance:
                raise ValueError('Active balance-tracking leave type required for category ' + code + '.')
            if row.code != CANONICAL_TYPES[code]:
                raise ValueError('Canonical leave category changed; reviewed evidence is stale.')
            types[code] = [row.id, row.code]
        for raw in db.scalars(select(AuditLog.new_value).where(
                AuditLog.action == RECEIPT_ACTION, AuditLog.entity_type == 'leave_opening_selection')):
            if _receipt_body(raw)['employee_id'] == employee_id:
                raise ValueError('Existing opening receipt; use a reviewed adjustment.')
        ids = list(type_map.values())
        ledger = db.scalar(select(LeaveBalanceTransaction.id).where(
            LeaveBalanceTransaction.employee_id == employee_id,
            LeaveBalanceTransaction.leave_type_id.in_(ids)).limit(1))
        if ledger is not None:
            raise ValueError('Existing ledger evidence for these categories; not treated as equivalent. Use a reviewed adjustment.')
        used = db.scalar(select(LeaveRecord.id).where(
            LeaveRecord.employee_id == employee_id, LeaveRecord.leave_type_id.in_(ids),
            LeaveRecord.status != 'cancelled').limit(1))
        if used is not None:
            raise ValueError('Existing leave records for these categories; not treated as already reflected.')
        # Register codes are day facts, not evidence of a ledger posting. The
        # selected basis-date projection counts them once; creating the opening
        # must not silently delete, repost or deem those facts already spent.
    return {'employee': [employee.id, employee.code], 'types': types}


def _plan(db, *, selection_id, type_map, **identity):
    reviews, _policy, source_digest = read_confirmed_opening_source(db, selection_id=selection_id, **identity)
    if [r['code'] for r in reviews] != ['A', 'W', 'H']:
        raise ValueError('Complete A/W/H confirmed source required.')
    employee_id = reviews[0]['employee_id']
    effective = {r['effective_start'] for r in reviews}
    reason = {r['reason'] for r in reviews}
    if len(effective) != 1 or len(reason) != 1 or {r['employee_id'] for r in reviews} != {employee_id}:
        raise ValueError('Confirmed source must share employee, basis date and reason.')
    effective_start = date.fromisoformat(next(iter(effective)))
    amounts = {}
    for review in reviews:
        amount = Decimal(review['days'])
        if not amount.is_finite() or amount < 0:
            raise ValueError('Manual opening must be finite and nonnegative.')
        if amount != 0:
            validate_manual_balance_change('opening', amount)
            preview_manual_balance(Decimal(0), 'opening', amount)
        amounts[review['code']] = review['days']
    _barrier(db, effective_start)
    state = _evidence(db, employee_id=employee_id, type_map=type_map)
    payload = {'selection_id': selection_id, 'source_sha256': source_digest,
        'restore_epoch': identity['expected_epoch'], 'employee_id': employee_id,
        'effective_start': effective_start.isoformat(), 'reason': next(iter(reason)),
        'amounts': amounts, 'state': state}
    return payload, sha256(_canonical(payload).encode()).hexdigest(), reviews


@bounded_workbook_operation
def review_opening_apply(db, *, selection_id, type_map, **identity):
    """Read-only review of exactly what an apply would post. Refuses on conflicts."""
    payload, digest, _ = _plan(db, selection_id=selection_id, type_map=_type_map(type_map), **identity)
    return {'apply_sha256': digest, 'plan': payload, 'ledger_applied': False}


def _receipt(db, selection_id):
    row = db.scalar(select(AuditLog).where(AuditLog.action == RECEIPT_ACTION,
        AuditLog.entity_type == 'leave_opening_selection', AuditLog.entity_id == selection_id)
        .order_by(AuditLog.id).limit(1).execution_options(populate_existing=True))
    return row


def _stored(db, row, type_map):
    body = _receipt_body(row.new_value)
    entries = db.execute(select(LeaveBalanceTransaction.id, LeaveBalanceTransaction.days_delta,
        LeaveBalanceTransaction.leave_type_id, LeaveBalanceTransaction.source_operation_key).where(
        LeaveBalanceTransaction.source_operation_key.like('manual_opening:' + row.entity_id + ':%'))
        .order_by(LeaveBalanceTransaction.id)).all()
    if body.get('type_ids') != type_map:
        raise ValueError('Replay type map differs from the stored receipt.')
    rows_ok = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(
        LeaveBalanceTransaction.id.in_(body['ledger_ids']),
        LeaveBalanceTransaction.employee_id == body['employee_id'],
        LeaveBalanceTransaction.transaction_type == 'opening',
        LeaveBalanceTransaction.source_digest == body['apply_sha256'],
        LeaveBalanceTransaction.leave_type_id.in_(list(type_map.values()))))
    if rows_ok != len(body['ledger_ids']):
        raise ValueError('Stored receipt does not match ledger evidence.')
    expected = {code: v for code, v in body['amounts'].items() if Decimal(v) != 0}
    got = {e.source_operation_key.rsplit(':', 1)[1]: format(e.days_delta, 'f') for e in entries}
    if got != {c: format(Decimal(v), 'f') for c, v in expected.items()}:
        raise ValueError('Stored receipt does not match ledger evidence.')
    return {'receipt_audit_id': row.id, 'apply_sha256': body['apply_sha256'],
            'ledger_ids': [e.id for e in entries], 'replayed': True}


@bounded_workbook_operation
def apply_confirmed_opening(db, *, selection_id, type_map, reviewed_apply_sha256, reason,
                            human_confirmed, **identity):
    """Post the three-category opening once, atomically; exact retry returns the receipt."""
    account = _guard(db, **identity)
    type_map = _type_map(type_map)
    if human_confirmed is not True or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError('Explicit human confirmation and bounded reason required.')
    if (not isinstance(reviewed_apply_sha256, str) or len(reviewed_apply_sha256) != 64
            or any(c not in '0123456789abcdef' for c in reviewed_apply_sha256)):
        raise ValueError('Reviewed apply digest required.')
    if not isinstance(selection_id, str) or not selection_id:
        raise ValueError('Confirmed selection id required.')
    previous = _receipt(db, selection_id)
    if previous is not None:
        if _receipt_body(previous.new_value)['apply_sha256'] != reviewed_apply_sha256:
            raise ValueError('Selection was already applied with different reviewed evidence.')
        return _stored(db, previous, type_map)
    # Table intents in alphabetical order, then the employee row, as create_leave_draft does.
    if db.get_bind().dialect.name != 'postgresql':
        raise RuntimeError('This kernel requires PostgreSQL locking semantics.')
    # Every table the digest or the collision checks read, alphabetical, before reading evidence.
    # Stale concurrent evidence that commits first makes this refuse; an apply that commits first
    # makes the other writer wait. The credential gate above is already held; the bounded lock
    # timeout turns a stuck wait into a refusal with nothing written.
    db.execute(text('LOCK TABLE audit_logs, employees, leave_balance_transactions, leave_month_close, '
                    'leave_records, leave_register_entries, leave_types, payroll_items, payroll_runs '
                    'IN SHARE ROW EXCLUSIVE MODE'))
    previous = _receipt(db, selection_id)
    if previous is not None:
        if _receipt_body(previous.new_value)['apply_sha256'] != reviewed_apply_sha256:
            raise ValueError('Selection was already applied with different reviewed evidence.')
        return _stored(db, previous, type_map)
    payload, digest, _reviews = _plan(db, selection_id=selection_id, type_map=type_map, **identity)
    db.execute(select(Employee.id).where(Employee.id == payload['employee_id']).with_for_update(key_share=True))
    if digest != reviewed_apply_sha256:
        raise ValueError('Reviewed opening evidence is stale; review again.')
    effective = date.fromisoformat(payload['effective_start'])
    ids = []
    with db.begin_nested():
        for code in CODES:
            amount = Decimal(payload['amounts'][code])
            if amount == 0:
                continue
            entry = LeaveBalanceTransaction(employee_id=payload['employee_id'], leave_type_id=type_map[code],
                transaction_type='opening', days_delta=amount,
                reason='Manual HR opening: ' + payload['reason'], created_by=account.username,
                source_operation_key=f'manual_opening:{selection_id}:{code}', source_digest=digest,
                earning_year=None, released_on=None)
            db.add(entry)
            db.flush()
            ids.append(entry.id)
            db.add(AuditLog(actor=account.username, action='leave_balance_opening',
                entity_type='employee_leave_balance', entity_id=f"{payload['employee_id']}:{type_map[code]}",
                old_value='0', new_value=format(amount, 'f'), reason=reason.strip()))
        receipt = AuditLog(actor=account.username, action=RECEIPT_ACTION,
            entity_type='leave_opening_selection', entity_id=selection_id, old_value=reviewed_apply_sha256,
            new_value=_canonical({'apply_sha256': digest, 'amounts': payload['amounts'],
                'type_ids': type_map, 'employee_id': payload['employee_id'],
                'source_sha256': payload['source_sha256'], 'ledger_ids': ids}),
            reason=reason.strip())
        db.add(receipt)
        db.flush()
    return {'receipt_audit_id': receipt.id, 'apply_sha256': digest, 'ledger_ids': ids, 'replayed': False}
