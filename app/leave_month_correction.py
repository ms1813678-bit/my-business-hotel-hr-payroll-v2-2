"""Reviewed correction of a POSTED leave month by reversal + replacement (Mo's answer 2 to the 12-question batch, relayed by Main:
reversing-entry correction. The module design and the limits below are technical settlements under Main's delegation).

Scope of this first version (everything else fails closed):
* earned lines only (A released, W, H). Usage lines depend on register-day attribution and are refused.
* a held annual line (never credited) cannot be corrected here; release it first.
* a downward correction is refused when the projected balance of that leave type would fall below zero, i.e. below
  what the ledger already consumed (usage, cash-out, zero decision, carry); the balance is read from the full ledger.
* only while the leave month is not closed and no approved/closed payroll overlaps the month (same gates as earning).
* the original receipt and its ledger rows are never touched; each correction adds a reversal row
  (the negative of the line's current effective amount) and a replacement row (the corrected amount), under a
  month_correction:<operation>:<code>:<reversal|replacement> key, with one immutable receipt and one audit row.
* review and apply are separate; apply needs the reviewed digest, human confirmation and a written reason.
The corrected amount is entered by HR (reviewed against the old effective amount); this module does not recompute
tier or age evidence. No payroll, cash or posting_allowed change."""
import json
import re
from calendar import monthrange
from datetime import date, datetime
from fractions import Fraction

from sqlalchemy import select

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_exact_amount import as_fraction, canonical_amount, ledger_amount, storage_amount
from app.leave_month_earning import CATEGORIES, _locks, _verify_receipt, canonical, digest
from app.leave_workflow import leave_balance
from app.models import (CAIRO_TZ, AuditLog, LeaveBalanceTransaction, LeaveMonthClose, LeaveMonthCorrection,
                        LeaveMonthEarning, LeaveType, PayrollRun)

HEX64 = re.compile('[0-9a-f]{64}')
IDENTITY = ('actor_id', 'session_id', 'session_version', 'expected_epoch')


def _kinds(db):
    return {c: db.scalar(select(LeaveType.id).where(LeaveType.code == code)) for c, code in CATEGORIES.items()}


def _effective(db, employee_id, month, code, kinds):
    """Current effective earned amount of one line, or None when the line was never credited (held)."""
    base = f'month_earning:{employee_id}:{month}:{code}:earned'
    rows = db.scalars(select(LeaveBalanceTransaction).where(
        LeaveBalanceTransaction.employee_id == employee_id, LeaveBalanceTransaction.leave_type_id == kinds[code],
        LeaveBalanceTransaction.source_operation_key == base)).all()
    total = sum((as_fraction(ledger_amount(r)) for r in rows), Fraction())
    corrections = db.scalars(select(LeaveMonthCorrection).where(
        LeaveMonthCorrection.employee_id == employee_id, LeaveMonthCorrection.month_start == date.fromisoformat(month))
        .order_by(LeaveMonthCorrection.sequence)).all()
    touched = False
    for row in corrections:
        for item in json.loads(row.result_json)['ledger']:
            if item['category'] == code:
                total += Fraction(item['amount']) * (-1 if item['role'] == 'reversal' else 1)
                touched = True
    if not rows and not touched:
        return None
    return total


def _gates(db, employee_id, month_start):
    end = date(month_start.year, month_start.month, monthrange(month_start.year, month_start.month)[1])
    if db.scalar(select(LeaveMonthClose.id).where(LeaveMonthClose.year == month_start.year, LeaveMonthClose.month == month_start.month)):
        raise ValueError('Closed leave month refuses correction.')
    if db.scalar(select(PayrollRun.id).where(PayrollRun.period_start <= end, PayrollRun.period_end >= month_start,
                                             PayrollRun.status.in_(('approved', 'closed')))):
        raise ValueError('Approved/closed payroll refuses correction.')


def _plan(db, *, employee_id, month_start, corrected_earned, evaluated_on, reason, identity):
    if type(employee_id) is not int or type(month_start) is not date or month_start.day != 1:
        raise ValueError('Explicit employee and posted calendar month required.')
    if (type(evaluated_on) is not date or evaluated_on > datetime.now(CAIRO_TZ).date()
            or evaluated_on < date(month_start.year, month_start.month, monthrange(month_start.year, month_start.month)[1])):
        raise ValueError('Current evaluation date after the month required.')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError('Written reason required.')
    if not isinstance(corrected_earned, dict) or not corrected_earned:
        raise ValueError('Corrected earned amounts required.')
    _locks(db, identity)
    _gates(db, employee_id, month_start)
    receipt = db.scalar(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id == employee_id,
                        LeaveMonthEarning.month_start == month_start).execution_options(populate_existing=True))
    if receipt is None:
        raise ValueError('Posted month receipt required; nothing to correct.')
    _verify_receipt(db, receipt)
    kinds = _kinds(db)
    month = month_start.isoformat()
    lines = []
    for code in sorted(corrected_earned):
        if code not in CATEGORIES:
            raise ValueError('Unknown leave category; usage lines are not correctable here.')
        new = corrected_earned[code]
        if not isinstance(new, Fraction) or new < 0:
            raise ValueError('Corrected amount must be an exact nonnegative Fraction.')
        old = _effective(db, employee_id, month, code, kinds)
        if old is None:
            raise ValueError('Held annual line was never credited; release it before any correction.')
        if new == old:
            continue
        if new < old:
            projected = leave_balance(db, employee_id, kinds[code]) + (new - old)
            if projected < 0:
                raise ValueError('Correction would drop the balance below what is already consumed '
                                 '(cash-out, zero decision, carry or usage); reverse that consumption first.')
        lines.append({'code': code, 'leave_type_id': kinds[code], 'old': canonical_amount(old), 'new': canonical_amount(new)})
    if not lines:
        raise ValueError('Corrected amounts equal the current amounts; nothing to correct.')
    sequence = 1 + len(db.scalars(select(LeaveMonthCorrection.id).where(LeaveMonthCorrection.original_receipt_id == receipt.id)).all())
    return {'kind': 'leave_month_correction', 'employee_id': employee_id, 'month_start': month,
            'original_receipt_id': receipt.id, 'original_result_digest': receipt.result_digest, 'sequence': sequence,
            'lines': lines, 'evaluated_on': evaluated_on.isoformat(), 'reason': reason.strip(), 'usage_unchanged': True}


@bounded_workbook_operation
def review_month_correction(db, *, employee_id, month_start, corrected_earned, evaluated_on, reason, **identity):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean transaction required.')
    if set(identity) != set(IDENTITY):
        raise ValueError('Full current identity required.')
    plan = _plan(db, employee_id=employee_id, month_start=month_start, corrected_earned=corrected_earned,
                 evaluated_on=evaluated_on, reason=reason, identity=identity)
    return {'plan': plan, 'reviewed_digest': digest(plan), 'posted': False, 'posting_allowed': False}


def verify_correction_receipt(db, row):
    result = json.loads(row.result_json)
    if digest(json.loads(row.plan_json)) != row.source_digest or digest(result) != row.result_digest:
        raise ValueError('Month correction receipt integrity mismatch.')
    if db.get(LeaveMonthEarning, row.original_receipt_id, populate_existing=True) is None:
        raise ValueError('Month correction lost its original receipt.')
    for item in result['ledger']:
        ledger = db.get(LeaveBalanceTransaction, item['id'], populate_existing=True)
        if (ledger is None or ledger.source_operation_key != item['key'] or ledger.source_digest != row.source_digest
                or ledger.employee_id != row.employee_id or ledger.leave_type_id != item['leave_type_id']
                or ledger.transaction_type != 'adjustment' or ledger.earning_year != row.month_start.year
                or canonical_amount(ledger_amount(ledger)) != item['signed']):
            raise ValueError('Month correction ledger changed.')
    audit = db.scalars(select(AuditLog).where(AuditLog.action == 'leave_month_corrected',
                       AuditLog.entity_type == 'leave_month_correction', AuditLog.entity_id == row.id)).all()
    if len(audit) != 1 or audit[0].new_value != row.result_json or audit[0].old_value != row.source_digest:
        raise ValueError('Month correction audit proof changed.')
    return result


def verified_correction_ledger_ids(db, employee_id):
    ids = set()
    for row in db.scalars(select(LeaveMonthCorrection).where(LeaveMonthCorrection.employee_id == employee_id)).all():
        ids.update(item['id'] for item in verify_correction_receipt(db, row)['ledger'])
    return ids


@bounded_workbook_operation
def apply_month_correction(db, *, operation_id, reviewed_digest, human_confirmed, reason, employee_id, month_start,
                           corrected_earned, evaluated_on, **identity):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean transaction required.')
    if (not isinstance(operation_id, str) or HEX64.fullmatch(operation_id) is None
            or not isinstance(reviewed_digest, str) or HEX64.fullmatch(reviewed_digest) is None
            or human_confirmed is not True or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000):
        raise ValueError('Explicit reviewed operation, confirmation and written reason required.')
    if set(identity) != set(IDENTITY):
        raise ValueError('Full current identity required.')
    actor = _locks(db, identity)
    request = {'employee_id': employee_id, 'month_start': month_start.isoformat() if type(month_start) is date else None,
               'corrected': sorted((c, canonical_amount(v)) for c, v in corrected_earned.items()) if isinstance(corrected_earned, dict) else None,
               'evaluated_on': evaluated_on.isoformat() if type(evaluated_on) is date else None, 'reason': reason.strip(),
               'reviewed_digest': reviewed_digest,
               'authority': {'actor_id': actor.id, 'session_id': identity['session_id'],
                             'session_version': identity['session_version'], 'restore_epoch': identity['expected_epoch']}}
    request_digest = digest(request)
    existing = db.get(LeaveMonthCorrection, operation_id, populate_existing=True)
    if existing is not None:
        if (existing.request_digest != request_digest or existing.actor_id != actor.id
                or existing.session_id != identity['session_id'] or existing.session_version != identity['session_version']
                or existing.restore_epoch != identity['expected_epoch']):
            raise ValueError('Month correction operation identity changed.')
        return verify_correction_receipt(db, existing)
    plan = _plan(db, employee_id=employee_id, month_start=month_start, corrected_earned=corrected_earned,
                 evaluated_on=evaluated_on, reason=reason, identity=identity)
    if digest(plan) != reviewed_digest:
        raise ValueError('Correction evidence changed; review again.')
    result = {'employee_id': employee_id, 'month_start': plan['month_start'], 'sequence': plan['sequence'], 'ledger': []}
    with db.begin_nested():
        for line in plan['lines']:
            old, new = Fraction(line['old']), Fraction(line['new'])
            for role, signed in (('reversal', -old), ('replacement', new)):
                if not signed:
                    continue
                key = f"month_correction:{operation_id}:{line['code']}:{role}"
                row = LeaveBalanceTransaction(employee_id=employee_id, leave_type_id=line['leave_type_id'],
                    transaction_type='adjustment', **storage_amount(signed), reason=reason.strip(),
                    created_by=actor.username, source_operation_key=key, source_digest=reviewed_digest,
                    earning_year=month_start.year, released_on=evaluated_on)
                db.add(row); db.flush()
                result['ledger'].append({'id': row.id, 'key': key, 'category': line['code'], 'role': role,
                    'leave_type_id': line['leave_type_id'], 'signed': canonical_amount(signed), 'amount': canonical_amount(abs(signed))})
        db.add(LeaveMonthCorrection(id=operation_id, employee_id=employee_id, month_start=month_start,
            original_receipt_id=plan['original_receipt_id'], sequence=plan['sequence'], actor_id=actor.id,
            session_id=identity['session_id'], session_version=identity['session_version'], restore_epoch=identity['expected_epoch'],
            source_digest=reviewed_digest, request_digest=request_digest, plan_json=canonical(plan),
            result_json=canonical(result), result_digest=digest(result)))
        db.add(AuditLog(actor=actor.username, action='leave_month_corrected', entity_type='leave_month_correction',
            entity_id=operation_id, old_value=reviewed_digest, new_value=canonical(result), reason=reason.strip()))
        db.flush()
    return result
