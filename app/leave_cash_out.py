"""Explicit manual leave cash-out: caller-owned transaction, exact debit + receipt.

One vertical only. No automatic cash-out, no payment, no payroll approval/close,
no money transfer: a successful apply posts exactly one exact ledger debit and
one immutable ``leave_cash_outs`` receipt recording a reviewed liability, plus
its audit row, all inside the caller's transaction (all or none).

Eligible balance is source-bound, never a forecast or an all-time arbitrary sum:
every ledger row for the (employee, leave type) pair must verify against
persisted evidence before any amount is eligible —

* ``manual_opening:`` rows must belong to a verified manual-opening receipt
  (``verified_receipt_ledger_ids``);
* ``month_earning:`` rows must belong to a digest/audit-verified
  ``LeaveMonthEarning`` receipt (annual rows under that key exist only after
  release, so unreleased held annual — which is never credited to the ledger —
  can never be cashed);
* ``leave_cashout:`` rows must belong to a verified receipt of this module;
* ``leave_use`` / ``leave_reversal`` rows must match a confirmed/cancelled
  ``LeaveRecord`` of the same employee/type and exact charged days;
* unkeyed ``opening`` / ``adjustment`` rows (the legacy ``add_balance_entry``
  path, the only writer for custom tracked types) must match that writer's
  audit chain exactly: one audit row whose old/new running balances bracket
  this row in ledger-id order.

Any other row refuses the whole pair: "existing unrelated ledger requires
separate reviewed reconciliation" (same posture as ``leave_month_earning``).

Cash basis is one explicitly selected ``PayrollItem``: basic salary is the
item's ``salary_basis`` snapshot (the employee basic salary frozen at run
creation in ``payroll_workflow``; ``base_salary`` is the prorated payable and
is NOT the basis) plus the item's typed ``performance_bonus`` only, divided by
30 via ``app.leave_cash_math``. The item/run identity, run status, item
``updated_at`` (the only revision surrogate the supplied ``PayrollItem`` model
carries — there is no revision column), period and both amounts are bound into
the source digest and re-verified on apply, so a changed basis refuses as
stale. The payroll rows themselves are never modified, whatever their status.
"""
import json
import re
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256

from sqlalchemy import select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_cash_math import calculate_leave_cash, cash_fits_numeric12_2
from app.leave_exact_amount import as_fraction, canonical_amount, ledger_amount, storage_amount
from app.leave_opening_receipt import verified_receipt_ledger_ids
from app.models import (
    CAIRO_TZ,
    AuditLog,
    Employee,
    LeaveBalanceTransaction,
    LeaveCashOut,
    LeaveMonthEarning,
    LeaveRecord,
    LeaveType,
    PayrollItem,
    PayrollRun,
)

HEX64 = re.compile(r'^[0-9a-f]{64}$')
IDENTITY_KEYS = ('actor_id', 'session_id', 'session_version', 'expected_epoch')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def parse_cash_days(value):
    """Exact days from a Decimal, a Fraction, or bounded text ('1.5' or '7/3')."""
    if isinstance(value, (Decimal, Fraction)):
        return as_fraction(value)
    if isinstance(value, str) and value.strip() and len(value) <= 130:
        try:
            return as_fraction(Fraction(value.strip()))
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError('Exact cash days required (decimal or numerator/denominator).') from exc
    raise ValueError('Exact cash days required (decimal or numerator/denominator).')


def _locks(db, identity):
    if db.get_bind().dialect.name != 'postgresql':
        raise ValueError('Leave cash-out requires migrated PostgreSQL.')
    # Exclusive-first: the credential/restore gate (_guard) runs BEFORE any
    # table lock below, matching the confirmed-source route pattern.
    actor = _guard(db, **identity)
    db.execute(text('LOCK TABLE audit_logs,employees,leave_balance_transactions,leave_cash_outs,'
                    'leave_types,payroll_items,payroll_runs IN SHARE ROW EXCLUSIVE MODE'))
    db.execute(text('LOCK TABLE leave_records,leave_month_earnings IN SHARE MODE'))
    return actor


def _verify_month_receipt(db, row):
    """Same canonical proof carry/completed-year use: digests, ledger identity, year, release date, one audit."""
    from app.leave_month_earning import _verify_receipt
    return _verify_receipt(db, row)


def _verify_cash_receipt(db, row):
    if digest(json.loads(row.source_json)) != row.source_digest or digest(json.loads(row.result_json)) != row.result_digest:
        raise ValueError('Cash-out receipt integrity mismatch.')
    source = json.loads(row.source_json)
    result = json.loads(row.result_json)
    if (source['employee_id'] != row.employee_id or source['leave_type_id'] != row.leave_type_id
            or source['basis']['payroll_item_id'] != row.payroll_item_id
            or source['basis']['payroll_run_id'] != row.payroll_run_id
            or source['days'] != row.days_canonical or source['cash']['cash_egp'] != format(row.cash_egp, 'f')
            or source['cash']['basic_salary'] != format(row.basic_salary, 'f')
            or source['cash']['performance_bonus'] != format(row.performance_bonus, 'f')
            or result['ledger_id'] != row.ledger_id):
        raise ValueError('Cash-out receipt fields disagree with its bound source/result.')
    ledger = db.get(LeaveBalanceTransaction, row.ledger_id, populate_existing=True)
    if (ledger is None or ledger.source_operation_key != f'leave_cashout:{row.id}'
            or ledger.source_digest != row.source_digest or ledger.employee_id != row.employee_id
            or ledger.leave_type_id != row.leave_type_id or ledger.transaction_type != 'adjustment'
            or canonical_amount(ledger_amount(ledger)) != result['ledger'][0]['amount']):
        raise ValueError('Cash-out ledger result changed.')
    # Bind the debit to the reviewed source itself, not only to a self-consistent result copy.
    if (source['evaluated_on'] != row.evaluated_on.isoformat()
            or ledger.earning_year != source.get('source_year', row.evaluated_on.year) or ledger.released_on != row.evaluated_on
            or as_fraction(ledger_amount(ledger)) != -Fraction(source['days'])
            or Fraction(source['days']) != Fraction(row.days_canonical)
            or result['ledger'][0]['id'] != ledger.id or len(result['ledger']) != 1):
        raise ValueError('Cash-out ledger debit disagrees with its reviewed source days or date.')
    audit = db.scalars(select(AuditLog).where(AuditLog.action == 'leave_cash_out_applied',
        AuditLog.entity_type == 'leave_cash_out', AuditLog.entity_id == row.id)).all()
    if len(audit) != 1 or audit[0].new_value != row.result_json or audit[0].old_value != row.source_digest:
        raise ValueError('Cash-out audit proof changed.')
    if audit[0].actor != ledger.created_by or (audit[0].reason or '') != (ledger.reason or ''):
        raise ValueError('Cash-out audit actor or reason disagrees with the ledger debit.')
    return result


def verified_released_balance(db, *, employee_id, leave_type_id, identity):
    """Exact verified released balance for one pair, or raise. Held excluded.

    Returns {'balance': Fraction, 'ledger_ids': [...], 'held_excluded': Fraction}.
    Caller must already hold the cash-out locks (see _locks).
    """
    if set(identity) != set(IDENTITY_KEYS):
        raise ValueError('Full current identity required.')
    with db.no_autoflush:
        rows = db.scalars(select(LeaveBalanceTransaction).where(
            LeaveBalanceTransaction.employee_id == employee_id,
            LeaveBalanceTransaction.leave_type_id == leave_type_id)
            .order_by(LeaveBalanceTransaction.id).execution_options(populate_existing=True)).all()
        if not rows:
            raise ValueError('No verified released balance source for this employee and leave type.')
        month_receipts = db.scalars(select(LeaveMonthEarning).where(
            LeaveMonthEarning.employee_id == employee_id)).all()
        month_ledger_ids, held = set(), Fraction()
        ledger_keys = {r.source_operation_key for r in rows}
        leave_type = db.get(LeaveType, leave_type_id, populate_existing=True)
        for receipt in month_receipts:
            result = _verify_month_receipt(db, receipt)
            month_ledger_ids.update(item['id'] for item in result['ledger'])
            if leave_type is not None and leave_type.code == 'annual':
                # Held annual was never credited; count it only to prove the
                # exclusion in the source snapshot, never as eligible balance.
                key = f'month_earning:{employee_id}:{receipt.month_start.isoformat()}:A:earned'
                if key not in ledger_keys:
                    held += Fraction(result['held_annual'])
        from app.leave_zero_decision import verified_zero_ledger_ids
        zero_ledger_ids=verified_zero_ledger_ids(db,employee_id)
        cash_ledger_ids = set()
        for receipt in db.scalars(select(LeaveCashOut).where(LeaveCashOut.employee_id == employee_id)).all():
            _verify_cash_receipt(db, receipt)
            cash_ledger_ids.add(receipt.ledger_id)
        from app.models import LeaveCarryExpiry
        from app.leave_carry_expiry import verify_expiry_receipt
        expiry_ids=set()
        for expiry in db.scalars(select(LeaveCarryExpiry).where(LeaveCarryExpiry.employee_id==employee_id,
                LeaveCarryExpiry.leave_type_id==leave_type_id)).all():
            verify_expiry_receipt(db,expiry);expiry_ids.add(expiry.ledger_id)
        audits = db.scalars(select(AuditLog).where(AuditLog.entity_type == 'employee_leave_balance',
            AuditLog.entity_id == f'{employee_id}:{leave_type_id}')).all()
        running = Fraction()
        for row in rows:
            amount = as_fraction(ledger_amount(row))
            key = row.source_operation_key
            if key is not None and key.startswith('manual_opening:'):
                selection = key.split(':')[1] if len(key.split(':')) == 3 else ''
                bound, unverified = verified_receipt_ledger_ids(
                    db, ledger_rows={row.id: key}, selection_ids={selection}, **identity)
                if row.id not in bound or row.id in unverified:
                    raise ValueError('Manual opening ledger is not verified released source.')
            elif key is not None and key.startswith('month_earning:'):
                if row.id not in month_ledger_ids:
                    raise ValueError('Monthly ledger is not verified released source.')
            elif key is not None and key.startswith('leave_zero:'):
                if row.id not in zero_ledger_ids:raise ValueError('Zero exclusion ledger is not verified.')
            elif key is not None and key.startswith('leave_cashout:'):
                if row.id not in cash_ledger_ids:
                    raise ValueError('Prior cash-out ledger is not verified.')
            elif key is not None and key.startswith('leave_carry_expiry:'):
                if row.id not in expiry_ids:raise ValueError('Grant exclusion ledger is not verified.')
            elif key is None and row.transaction_type in ('leave_use', 'leave_reversal') and row.leave_record_id is not None:
                record = db.get(LeaveRecord, row.leave_record_id, populate_existing=True)
                expected_status = 'confirmed' if row.transaction_type == 'leave_use' else 'cancelled'
                if (record is None or record.employee_id != employee_id or record.leave_type_id != leave_type_id
                        or record.status != expected_status
                        or as_fraction(record.charged_days) != abs(amount)
                        or (row.transaction_type == 'leave_use') != (amount < 0)):
                    raise ValueError('Leave record ledger is not verified source.')
            elif key is None and row.transaction_type in ('opening', 'adjustment'):
                matches = []
                for audit in audits:
                    if audit.action != f'leave_balance_{row.transaction_type}' or audit.actor != row.created_by:
                        continue
                    try:
                        old, new = Decimal(audit.old_value), Decimal(audit.new_value)
                    except Exception:
                        continue
                    if as_fraction(new) - as_fraction(old) == amount and as_fraction(old) == running:
                        matches.append(audit)
                if len(matches) != 1:
                    raise ValueError('Unkeyed ledger lacks its exact audit-chain evidence; '
                                     'separate reviewed reconciliation required.')
            else:
                raise ValueError('Existing unrelated ledger requires separate reviewed reconciliation.')
            running += amount
    return {'balance': running, 'ledger_ids': [r.id for r in rows], 'held_excluded': held}


def _basis(db, *, employee, payroll_item_id):
    if type(payroll_item_id) is not int or payroll_item_id < 1 or payroll_item_id > 2147483647:
        raise ValueError('Explicit payroll item source selection required.')
    item = db.get(PayrollItem, payroll_item_id, populate_existing=True)
    if item is None or item.employee_id != employee.id:
        raise ValueError('Selected payroll item does not belong to this employee.')
    run = db.get(PayrollRun, item.payroll_run_id, populate_existing=True)
    if run is None or run.status not in ('draft', 'reopened', 'approved', 'closed'):
        raise ValueError('Selected payroll run is not a current source.')
    if item.employee_code_snapshot != employee.code:
        raise ValueError('Payroll item identity snapshot does not match the employee.')
    for amount in (item.salary_basis, item.performance_bonus):
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise ValueError('Payroll basis amounts must be finite nonnegative Decimals.')
    return {'payroll_item_id': item.id, 'payroll_run_id': run.id, 'run_status': run.status,
            'period_start': run.period_start.isoformat(), 'period_end': run.period_end.isoformat(),
            'pay_date': run.pay_date.isoformat(), 'basis_date': run.period_start.isoformat(),
            'item_updated_at': item.updated_at.isoformat() if item.updated_at else None,
            'basic_salary': format(item.salary_basis, 'f'),
            'performance_bonus': format(item.performance_bonus, 'f')}


def _derive(db, *, employee_id, leave_type_id, days, payroll_item_id, evaluated_on, identity, source_year=None):
    actor = _locks(db, identity)
    if type(employee_id) is not int or employee_id < 1 or type(leave_type_id) is not int or leave_type_id < 1:
        raise ValueError('Explicit employee and leave type required.')
    if type(evaluated_on) is not date or evaluated_on > datetime.now(CAIRO_TZ).date():
        raise ValueError('Explicit current evaluation date required.')
    if source_year is not None and (type(source_year) is not int or not 1 <= source_year < evaluated_on.year):
        raise ValueError('Completed explicit annual source year required.')
    employee = db.get(Employee, employee_id, populate_existing=True)
    if employee is None or employee.status != 'active':
        raise ValueError('Active employee required; deactivation refuses cash-out.')
    leave_type = db.get(LeaveType, leave_type_id, populate_existing=True)
    if leave_type is None or not leave_type.active or not leave_type.tracks_balance:
        raise ValueError('Active balance-tracked leave type required. Counters are not a balance.')
    exact_days = parse_cash_days(days)
    if exact_days <= 0:
        raise ValueError('Explicit positive exact cash days required.')
    source = verified_released_balance(db, employee_id=employee_id, leave_type_id=leave_type_id, identity=identity)
    if source['balance'] <= 0:
        raise ValueError('Negative or zero balance is debt/no balance, never eligible cash.')
    if exact_days > source['balance']:
        raise ValueError('Requested days exceed the verified released balance.')
    from app.leave_carry_consumption import refuse_unresolved_expired_carry_cash
    refuse_unresolved_expired_carry_cash(db,employee_id=employee_id,leave_type_id=leave_type_id,evaluated_on=evaluated_on,identity=identity)
    basis = _basis(db, employee=employee, payroll_item_id=payroll_item_id)
    calculation = calculate_leave_cash(days=exact_days, basic_salary=Decimal(basis['basic_salary']),
                                       performance_bonus=Decimal(basis['performance_bonus']))
    # Representability is refused at review, before any write exists.
    cash_fits_numeric12_2(calculation)
    payload = {'employee_id': employee_id, 'employee_code': employee.code,
               'leave_type_id': leave_type_id, 'leave_type_code': leave_type.code,
               'evaluated_on': evaluated_on.isoformat(), 'ledger_ids': source['ledger_ids'],
               'balance': canonical_amount(source['balance']),
               'held_excluded': canonical_amount(source['held_excluded']),
               'days': canonical_amount(exact_days), 'basis': basis,
               'cash': {'daily_rate_exact': canonical_amount(calculation.daily_rate_exact),
                        'cash_exact': canonical_amount(calculation.cash_exact),
                        'cash_egp': format(calculation.cash_egp, 'f'),
                        'basic_salary': basis['basic_salary'],
                        'performance_bonus': basis['performance_bonus']},
               'liability': 'recorded_not_paid'}
    if source_year is not None:
        payload['source_year'] = source_year
    return payload, actor


@bounded_workbook_operation
def review_cash_out(db, *, employee_id, leave_type_id, days, payroll_item_id, evaluated_on, source_year=None, **identity):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean transaction required.')
    payload, _actor = _derive(db, employee_id=employee_id, leave_type_id=leave_type_id, days=days,
                              payroll_item_id=payroll_item_id, evaluated_on=evaluated_on, identity=identity, source_year=source_year)
    return {'plan': payload, 'reviewed_digest': digest(payload), 'posted': False}


@bounded_workbook_operation
def apply_cash_out(db, *, operation_id, reviewed_digest, human_confirmed, reason,
                   employee_id, leave_type_id, days, payroll_item_id, evaluated_on, source_year=None, **identity):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean transaction required.')
    if (not isinstance(operation_id, str) or HEX64.match(operation_id) is None
            or not isinstance(reviewed_digest, str) or HEX64.match(reviewed_digest) is None
            or human_confirmed is not True or not isinstance(reason, str)
            or not reason.strip() or len(reason) > 1000):
        raise ValueError('Explicit reviewed operation, confirmation and bounded reason required.')
    if set(identity) != set(IDENTITY_KEYS):
        raise ValueError('Full current identity required.')
    actor = _locks(db, identity)
    days_fraction = parse_cash_days(days)
    request = {'employee_id': employee_id, 'leave_type_id': leave_type_id,
               'days': canonical_amount(days_fraction), 'payroll_item_id': payroll_item_id,
               'evaluated_on': evaluated_on.isoformat() if type(evaluated_on) is date else None,
               'reviewed_digest': reviewed_digest, 'reason': reason.strip(),
               'authority': {'actor_id': actor.id, 'session_id': identity['session_id'],
                             'session_version': identity['session_version'],
                             'restore_epoch': identity['expected_epoch']}}
    if source_year is not None:
        request['source_year'] = source_year
    request_digest = digest(request)
    existing = db.get(LeaveCashOut, operation_id, populate_existing=True)
    if existing is not None:
        if (existing.request_digest != request_digest or existing.actor_id != actor.id
                or existing.session_id != identity['session_id']
                or existing.session_version != identity['session_version']
                or existing.restore_epoch != identity['expected_epoch']):
            raise ValueError('Cash-out operation identity changed.')
        return _verify_cash_receipt(db, existing) and json.loads(existing.result_json)
    payload, actor = _derive(db, employee_id=employee_id, leave_type_id=leave_type_id, days=days_fraction,
                             payroll_item_id=payroll_item_id, evaluated_on=evaluated_on, identity=identity, source_year=source_year)
    source_digest = digest(payload)
    if source_digest != reviewed_digest:
        raise ValueError('Cash-out evidence changed; review again.')
    debit = -days_fraction
    key = f'leave_cashout:{operation_id}'
    result = {'employee_id': employee_id, 'ledger': [], 'cash_egp': payload['cash']['cash_egp'],
              'payroll_item_id': payroll_item_id, 'liability': 'recorded_not_paid'}
    with db.begin_nested():
        ledger = LeaveBalanceTransaction(employee_id=employee_id, leave_type_id=leave_type_id,
            transaction_type='adjustment', **storage_amount(debit), reason=reason.strip(),
            created_by=actor.username, source_operation_key=key, source_digest=source_digest,
            earning_year=source_year if source_year is not None else evaluated_on.year, released_on=evaluated_on)
        db.add(ledger)
        db.flush()
        result['ledger'].append({'id': ledger.id, 'key': key, 'leave_type_id': leave_type_id,
                                 'amount': canonical_amount(debit)})
        result['ledger_id'] = ledger.id
        receipt = LeaveCashOut(id=operation_id, employee_id=employee_id, leave_type_id=leave_type_id,
            payroll_item_id=payroll_item_id, payroll_run_id=payload['basis']['payroll_run_id'],
            evaluated_on=evaluated_on, days_canonical=payload['days'],
            daily_rate_canonical=payload['cash']['daily_rate_exact'],
            cash_exact_canonical=payload['cash']['cash_exact'],
            cash_egp=Decimal(payload['cash']['cash_egp']),
            basic_salary=Decimal(payload['cash']['basic_salary']),
            performance_bonus=Decimal(payload['cash']['performance_bonus']),
            ledger_id=ledger.id, actor_id=actor.id, session_id=identity['session_id'],
            session_version=identity['session_version'], restore_epoch=identity['expected_epoch'],
            source_digest=source_digest, request_digest=request_digest,
            source_json=canonical(payload), result_json=canonical(result), result_digest=digest(result))
        db.add(receipt)
        db.add(AuditLog(actor=actor.username, action='leave_cash_out_applied',
                        entity_type='leave_cash_out', entity_id=operation_id,
                        old_value=source_digest, new_value=canonical(result), reason=reason.strip()))
        db.flush()
    return result
