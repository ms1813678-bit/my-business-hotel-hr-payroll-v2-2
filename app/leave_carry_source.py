"""Read-only exact carry-source attribution. No grant, settlement or ledger write."""
from datetime import date, datetime
from hashlib import sha256
import json
from fractions import Fraction

from sqlalchemy import select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_exact_amount import canonical_amount, exact_sum, ledger_amount
from app.models import CAIRO_TZ, Employee, LeaveBalanceTransaction, LeaveMonthEarning, LeaveType, LeaveYearAttribution
from app.leave_month_earning import _verify_receipt
from app.leave_year_attribution_store import verify_year_attribution


@bounded_workbook_operation
def review_carry_source(db, *, employee_id, leave_type_id, source_year, reviewed_on, **identity):
    """Return explicit year-tagged ledger source, not an all-time year guess.

    A source-less manual opening or adjustment may cover old years. It cannot
    be assigned to this year merely because its creation timestamp matches.
    Zero unattributed openings do not have rows and therefore do not add a gap.
    Active balance-tracking custom leave types are supported by stable ID.
    Sick/absence counters are not silently turned into new banked balances.
    """
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean carry source transaction required.')
    if (type(employee_id) is not int or employee_id < 1
            or type(leave_type_id) is not int or leave_type_id < 1
            or type(source_year) is not int or not 1 <= source_year <= 9998
            or type(reviewed_on) is not date or reviewed_on.year < source_year
            or reviewed_on > datetime.now(CAIRO_TZ).date()):
        raise ValueError('Explicit source year, current review date, employee and type required.')
    _guard(db, **identity)
    db.execute(text('LOCK TABLE employees,leave_types,leave_balance_transactions IN SHARE MODE'))
    employee = db.get(Employee, employee_id, populate_existing=True)
    kind = db.get(LeaveType, leave_type_id, populate_existing=True)
    if employee is None or kind is None or not kind.active or not kind.tracks_balance:
        raise ValueError('Existing active balance-tracking employee/type source required.')
    rows = db.scalars(select(LeaveBalanceTransaction).where(
        LeaveBalanceTransaction.employee_id == employee_id,
        LeaveBalanceTransaction.leave_type_id == leave_type_id)
        .order_by(LeaveBalanceTransaction.id).limit(10001)).all()
    if len(rows) > 10000:
        raise ValueError('Carry ledger source exceeds bounded scope.')
    attributed = {}
    attribution_bindings = []
    for row in rows:
        if row.earning_year is None:
            receipt = db.scalar(select(LeaveYearAttribution).where(LeaveYearAttribution.ledger_id == row.id))
            if receipt is None:
                raise ValueError('Unattributed opening or adjustment requires separate reviewed year attribution.')
            verified = verify_year_attribution(db, receipt)
            attributed[row.id] = dict((y, Fraction(v)) for y, v in verified['allocations']).get(source_year, Fraction())
            attribution_bindings.append((receipt.id, receipt.source_digest, receipt.result_digest))
    selected = [row for row in rows if row.earning_year == source_year]
    # A year tag is not proof. Bind every selected ledger row to an existing
    # immutable monthly receipt and its audit, without manufacturing a balance.
    receipts = db.scalars(select(LeaveMonthEarning).where(
        LeaveMonthEarning.employee_id == employee_id).order_by(LeaveMonthEarning.month_start)
        .limit(10001)).all()
    if len(receipts) > 10000:
        raise ValueError('Carry monthly source exceeds bounded scope.')
    proven_ids = set()
    receipt_bindings = []
    held = []
    ledger_keys = {row.source_operation_key for row in rows}
    canonical = {'annual': 'A', 'weekly_rest': 'W', 'official_holiday': 'H'}
    code = canonical.get(kind.code)
    for receipt in receipts:
        result = _verify_receipt(db, receipt)
        source = json.loads(receipt.source_json)
        proven_ids.update(item['id'] for item in result['ledger'])
        receipt_bindings.append((receipt.id, receipt.source_digest, receipt.result_digest))
        if code == 'A' and receipt.month_start.year == source_year:
            key = f'month_earning:{employee_id}:{receipt.month_start.isoformat()}:A:earned'
            if key not in ledger_keys:
                held.append((receipt.id, canonical_amount(Fraction(source['earned']['A']))))
    from app.leave_month_correction import verified_correction_ledger_ids
    proven_ids.update(verified_correction_ledger_ids(db, employee_id))
    # Annual settlement cash debits retain their completed earning-source year,
    # independent of the later review/payment date. A manual all-time cash debit
    # has no such allocation proof and cannot be inferred into a carry year.
    from app.models import LeaveCashOut, LeaveAnnualSettlement
    for row in selected:
        if (row.source_operation_key or '').startswith('leave_cashout:'):
            key = row.source_operation_key.split(':', 1)[1]
            cash = db.get(LeaveCashOut, key, populate_existing=True)
            annual = db.get(LeaveAnnualSettlement, key, populate_existing=True)
            if (cash is None or annual is None or annual.cash_out_id != key
                    or annual.employee_id != employee_id or annual.leave_type_id != leave_type_id
                    or annual.source_year != source_year
                    or json.loads(cash.source_json).get('source_year') != source_year):
                raise ValueError('Cash debit lacks reviewed annual source-year allocation provenance.')
            from app.leave_annual_settlement_store import verify_annual_cash_allocation
            verify_annual_cash_allocation(db, annual, cash)
            proven_ids.add(row.id)
    for row in selected:
        if (row.source_operation_key or '').startswith('leave_zero:'):
            from app.leave_zero_decision import verify_zero_ledger_for_year
            verify_zero_ledger_for_year(db,row,employee_id=employee_id,leave_type_id=leave_type_id,source_year=source_year)
            proven_ids.add(row.id)
    from app.models import LeaveCarryExpiry
    from app.leave_carry_expiry import verify_expiry_receipt
    for expiry in db.scalars(select(LeaveCarryExpiry).where(LeaveCarryExpiry.employee_id==employee_id,
            LeaveCarryExpiry.leave_type_id==leave_type_id)).all():
        verify_expiry_receipt(db,expiry);proven_ids.add(expiry.ledger_id)
    if any(row.id not in proven_ids for row in selected):
        raise ValueError('Year-tagged ledger lacks verified monthly receipt provenance.')
    values = [(row.id, canonical_amount(ledger_amount(row)), row.transaction_type,
               row.source_operation_key, row.source_digest, row.earning_year,
               row.released_on.isoformat() if row.released_on else None) for row in rows]
    amount = exact_sum([ledger_amount(row) for row in selected] + list(attributed.values()))
    payload = dict(employee_id=employee_id, leave_type_id=leave_type_id,
                   leave_type_code=kind.code, tracks_balance=kind.tracks_balance,
                   active=kind.active, source_year=source_year,
                   reviewed_on=reviewed_on.isoformat(), rows=values,
                   monthly_receipts=receipt_bindings, year_attributions=attribution_bindings, held_annual=held,
                   selected_ids=[row.id for row in selected], amount=canonical_amount(amount))
    digest = sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'source': payload, 'source_digest': digest, 'amount': amount,
            'source_verified': False, 'unresolved': ('source_year_completeness_and_opening_attribution_not_verified',),
            'posting_allowed': False}
