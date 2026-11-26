"""Explicit HR zero decision for a completed-year weekly-rest/holiday leftover.

The explicit completed-year choice posts one exact exclusion debit and an
immutable receipt. Carry uses the existing allocation receipt. This zero choice
posts no cash; it does not prohibit a separate reviewed manual cash-out of a
remaining verified balance. Negative leftovers remain debt, never erased.
No FIFO, automatic expiry, netting or new role authority. Annual settlement and
custom-type decisions are outside this service. Caller owns commit.
"""
import json
import re
from datetime import date
from fractions import Fraction

from sqlalchemy import select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_exact_amount import as_fraction, canonical_amount, ledger_amount, storage_amount
from app.leave_month_earning import _locks, canonical, digest
from app.models import AuditLog, LeaveBalanceTransaction, LeaveCarryAllocation, LeaveZeroDecision

HEX64 = re.compile('[0-9a-f]{64}')
ZERO_KINDS = {'weekly_rest', 'official_holiday'}
IDENTITY_KEYS = ('actor_id', 'session_id', 'session_version', 'expected_epoch')


_SELECTION_KEYS = ('employee_id', 'leave_type_id', 'source_year', 'reviewed_on') + IDENTITY_KEYS


def _check_selection(source_selection, reason):
    if not isinstance(source_selection, dict) or any(k not in source_selection for k in _SELECTION_KEYS):
        raise ValueError('Complete explicit zero selection required.')
    if (type(source_selection['employee_id']) is not int or type(source_selection['leave_type_id']) is not int
            or type(source_selection['source_year']) is not int or not isinstance(reason, str)):
        raise ValueError('Explicit typed zero selection and text reason required.')
    if type(source_selection['reviewed_on']) is not date:
        raise ValueError('Explicit review date required.')


def _derive(db, *, source_selection, reason):
    _check_selection(source_selection, reason)
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError('Explicit bounded zero reason required.')
    from app.leave_recorded_year_view import review_later_year_source
    source = review_later_year_source(db, **source_selection)
    kind = source['view']['metadata_completed']['source']['leave_type_code']
    if kind not in ZERO_KINDS:
        raise ValueError('Zero decision applies only to weekly rest and official holiday leftovers.')
    amount = as_fraction(source['amount'])
    if amount < 0:
        raise ValueError('Negative leftover carries forward as debt; it is never zeroed.')
    if amount == 0:
        raise ValueError('No positive leftover to zero.')
    employee_id, leave_type_id = source_selection['employee_id'], source_selection['leave_type_id']
    year = source_selection['source_year']
    if db.scalar(select(LeaveCarryAllocation.id).where(LeaveCarryAllocation.employee_id == employee_id,
            LeaveCarryAllocation.leave_type_id == leave_type_id, LeaveCarryAllocation.source_year == year)):
        raise ValueError('Source year already carried; carry and zero are exclusive.')
    # A later debit may already have consumed part of this leftover. Which part is
    # unknown, so subtracting the whole amount could create false debt. Refuse.
    for row in db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id == employee_id,
            LeaveBalanceTransaction.leave_type_id == leave_type_id).order_by(LeaveBalanceTransaction.id)).all():
        later = row.earning_year is None or row.earning_year > year
        if later and as_fraction(ledger_amount(row)) < 0:
            raise ValueError('Later debit or usage exists; zeroing needs separate reviewed reconciliation.')
    return dict(action='zero', employee_id=employee_id, leave_type_id=leave_type_id, leave_type_code=kind,
        source_year=year, source_digest=source['source_digest'], source=source['source'],
        zeroed_amount=canonical_amount(amount), ledger_delta=canonical_amount(-amount),
        cash_amount='0/1', reviewed_on=source_selection['reviewed_on'].isoformat(),
        reason=reason.strip(), operational_spending_allowed=False)


@bounded_workbook_operation
def review_zero_decision(db, *, source_selection, reason):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean zero decision transaction required.')
    _check_selection(source_selection, reason)
    payload = _derive(db, source_selection=source_selection, reason=reason)
    return dict(plan=payload, reviewed_digest=digest(payload), posting_allowed=False)


def _verify(db, row):
    """Receipt, exclusion debit and audit must agree. No source re-derivation."""
    request = json.loads(row.request_json)
    result = json.loads(row.result_json)
    if (row.request_json != canonical(request) or row.request_digest != digest(request)
            or row.result_json != canonical(result) or row.result_digest != digest(result)
            or result['source_digest'] != row.source_digest or digest(result['source']) != row.source_digest
            or result['action'] != 'zero' or result['employee_id'] != row.employee_id
            or result['leave_type_id'] != row.leave_type_id or result['source_year'] != row.source_year
            or request['plan'] != result or request['reviewed_digest'] != digest(result)):
        raise ValueError('Zero decision identity or evidence changed.')
    if (result['leave_type_code'] not in ZERO_KINDS or result['cash_amount'] != '0/1'
            or result['operational_spending_allowed'] is not False
            or Fraction(result['zeroed_amount']) <= 0
            or result['ledger_delta'] != canonical_amount(-Fraction(result['zeroed_amount']))):
        raise ValueError('Zero decision amounts are not an exact positive exclusion.')
    ledger = db.get(LeaveBalanceTransaction, row.ledger_id, populate_existing=True)
    if (ledger is None or ledger.source_operation_key != f'leave_zero:{row.id}'
            or ledger.source_digest != row.source_digest or ledger.employee_id != row.employee_id
            or ledger.leave_type_id != row.leave_type_id or ledger.transaction_type != 'adjustment'
            or ledger.earning_year != row.source_year
            or ledger.released_on.isoformat() != result['reviewed_on']
            or canonical_amount(ledger_amount(ledger)) != result['ledger_delta']
            or ledger.reason != result['reason']):
        raise ValueError('Zero decision exclusion debit changed.')
    audits = db.scalars(select(AuditLog).where(AuditLog.action == 'leave_zero_decided',
        AuditLog.entity_type == 'leave_zero_decision', AuditLog.entity_id == row.id)).all()
    if (len(audits) != 1 or audits[0].actor != request['actor_username'] or audits[0].actor != ledger.created_by
            or audits[0].old_value != row.source_digest or audits[0].new_value != row.result_json
            or audits[0].reason != result['reason']):
        raise ValueError('Zero decision audit evidence changed.')
    # Later-year sources carry prior-grant attribution in their full v1 proof.
    # The legacy receipt contract is unchanged; never re-derive either source.
    if 'version' in result['source']:
        if result['source']['version'] != 'year2-recorded-v1':
            raise ValueError('Zero consumer recorded source version is not admitted.')
        from app.leave_recorded_year_view import read_verified_recorded_year_view
        view = read_verified_recorded_year_view(db,
            result=dict(source=result['source'], source_digest=result['source_digest'], source_year=row.source_year,
                        carried_amount=result['zeroed_amount'], held_annual=result['source']['held_annual']),
            employee_id=row.employee_id, leave_type_id=row.leave_type_id)
        if view['full_net'] != Fraction(result['zeroed_amount']):
            raise ValueError('Zero recorded source amount changed.')
    return result


def verified_zero_ledger_ids(db, employee_id):
    """Ledger ids of this employee's fully verified zero exclusion debits."""
    ids = set()
    for row in db.scalars(select(LeaveZeroDecision).where(LeaveZeroDecision.employee_id == employee_id)).all():
        _verify(db, row)
        ids.add(row.ledger_id)
    return ids


def verify_zero_ledger_for_year(db, ledger_row, *, employee_id, leave_type_id, source_year):
    """For source readers: this key-tagged debit belongs to a verified zero receipt of that year."""
    key = ledger_row.source_operation_key or ''
    receipt = db.get(LeaveZeroDecision, key.split(':', 1)[1], populate_existing=True) if key.startswith('leave_zero:') else None
    if (receipt is None or receipt.ledger_id != ledger_row.id or receipt.employee_id != employee_id
            or receipt.leave_type_id != leave_type_id or receipt.source_year != source_year):
        raise ValueError('Zero exclusion debit lacks its reviewed source-year receipt.')
    _verify(db, receipt)
    return True


@bounded_workbook_operation
def record_zero_decision(db, *, operation_id, reviewed_digest, human_confirmed, source_selection, reason):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean zero decision transaction required.')
    if (not isinstance(operation_id, str) or HEX64.fullmatch(operation_id) is None
            or not isinstance(reviewed_digest, str) or HEX64.fullmatch(reviewed_digest) is None
            or human_confirmed is not True):
        raise ValueError('Explicit confirmed reviewed zero operation required.')
    _check_selection(source_selection, reason)
    identity = {k: source_selection[k] for k in IDENTITY_KEYS}
    actor = _locks(db, identity)
    db.execute(text('LOCK TABLE leave_carry_allocations,leave_zero_decisions IN SHARE ROW EXCLUSIVE MODE'))
    previous = db.get(LeaveZeroDecision, operation_id, populate_existing=True)
    if previous is not None:
        request = json.loads(previous.request_json)
        if (request['reviewed_digest'] != reviewed_digest or request['actor_id'] != actor.id
                or request['session_id'] != identity['session_id']
                or request['session_version'] != identity['session_version']
                or request['restore_epoch'] != identity['expected_epoch']
                or request['plan']['employee_id'] != source_selection['employee_id']
                or request['plan']['leave_type_id'] != source_selection['leave_type_id']
                or request['plan']['source_year'] != source_selection['source_year']
                or request['plan']['reviewed_on'] != source_selection['reviewed_on'].isoformat()
                or request['plan']['reason'] != reason.strip()):
            raise ValueError('Zero decision operation identity changed.')
        return _verify(db, previous)
    payload = _derive(db, source_selection=source_selection, reason=reason)
    if digest(payload) != reviewed_digest:
        raise ValueError('Zero source changed; review again.')
    if db.scalar(select(LeaveZeroDecision.id).where(LeaveZeroDecision.employee_id == payload['employee_id'],
            LeaveZeroDecision.leave_type_id == payload['leave_type_id'],
            LeaveZeroDecision.source_year == payload['source_year'])):
        raise ValueError('Source year already zeroed; separate correction required.')
    request = dict(plan=payload, reviewed_digest=reviewed_digest, actor_id=actor.id, actor_username=actor.username,
        session_id=identity['session_id'], session_version=identity['session_version'],
        restore_epoch=identity['expected_epoch'])
    with db.begin_nested():
        ledger = LeaveBalanceTransaction(employee_id=payload['employee_id'], leave_type_id=payload['leave_type_id'],
            transaction_type='adjustment', **storage_amount(-Fraction(payload['zeroed_amount'])),
            reason=payload['reason'], created_by=actor.username, source_operation_key=f'leave_zero:{operation_id}',
            source_digest=payload['source_digest'], earning_year=payload['source_year'],
            released_on=source_selection['reviewed_on'])
        db.add(ledger)
        db.flush()
        row = LeaveZeroDecision(id=operation_id, employee_id=payload['employee_id'],
            leave_type_id=payload['leave_type_id'], source_year=payload['source_year'], ledger_id=ledger.id,
            request_digest=digest(request), source_digest=payload['source_digest'], result_digest=digest(payload),
            request_json=canonical(request), result_json=canonical(payload))
        db.add(row)
        db.add(AuditLog(actor=actor.username, action='leave_zero_decided', entity_type='leave_zero_decision',
            entity_id=operation_id, old_value=payload['source_digest'], new_value=row.result_json,
            reason=payload['reason']))
        db.flush()
    return _verify(db, row)
