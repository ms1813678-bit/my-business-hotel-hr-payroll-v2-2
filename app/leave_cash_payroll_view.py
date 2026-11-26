"""Items 2h and 2ذ, step one, isolated and read-only: verified leave cash liabilities of one payroll item.

Every cash payout of leave days, manual or from the annual settlement, is exactly one immutable leave_cash_outs
receipt with exactly one ledger debit (unique ledger_id). So one receipt table is the single source of truth and a
payroll link that reads it once per receipt cannot count a payout twice. This view re-verifies each receipt with the
cash-out kernel's own verifier and returns the exact total. It changes nothing, posts nothing, edits no payroll row,
does not decide where the amount appears in payroll, and does not mark anything paid.
"""
from decimal import Decimal

from sqlalchemy import select

from app.leave_cash_out import _verify_cash_receipt, digest
from app.models import LeaveCashOut, PayrollItem


def verified_cash_liabilities(db, *, payroll_item_id):
    if type(payroll_item_id) is not int or payroll_item_id < 1:
        raise ValueError('Explicit payroll item required.')
    item = db.get(PayrollItem, payroll_item_id)
    if item is None:
        raise ValueError('Payroll item not found.')
    receipts = db.scalars(select(LeaveCashOut).where(LeaveCashOut.payroll_item_id == payroll_item_id)
                          .order_by(LeaveCashOut.id)).all()
    rows, seen_ledgers = [], set()
    for r in receipts:
        _verify_cash_receipt(db, r)
        if r.employee_id != item.employee_id or r.payroll_run_id != item.payroll_run_id:
            raise ValueError('Cash-out receipt does not belong to this payroll item.')
        if r.ledger_id in seen_ledgers:
            raise ValueError('Duplicate ledger debit across receipts.')
        seen_ledgers.add(r.ledger_id)
        rows.append({'receipt_id': r.id, 'ledger_id': r.ledger_id, 'leave_type_id': r.leave_type_id,
                     'days': r.days_canonical, 'cash_egp': format(r.cash_egp, 'f')})
    total = sum((r.cash_egp for r in receipts), Decimal('0.00'))
    out = {'payroll_item_id': payroll_item_id, 'receipts': rows, 'total_cash_egp': format(total, 'f'),
           'liability': 'recorded_not_paid', 'posting_allowed': False, 'payroll_money_allowed': False}
    out['view_digest'] = digest(out)
    return out
