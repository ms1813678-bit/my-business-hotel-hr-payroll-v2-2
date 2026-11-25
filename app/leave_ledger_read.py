"""Shared authoritative one-ledger mixed exact amount reads. No SQL SUM(NULL)."""
from sqlalchemy import select

from app.leave_exact_amount import exact_sum, ledger_amount
from app.models import LeaveBalanceTransaction


def grouped_ledger_balances(db, *, employee_ids=None):
    statement = select(LeaveBalanceTransaction)
    if employee_ids is not None:
        statement = statement.where(LeaveBalanceTransaction.employee_id.in_(employee_ids))
    groups = {}
    with db.no_autoflush:
        for row in db.scalars(statement.order_by(LeaveBalanceTransaction.id)):
            groups.setdefault((row.employee_id,row.leave_type_id),[]).append(ledger_amount(row))
    return {key: exact_sum(values) for key,values in groups.items()}


def ledger_balance(db, employee_id, leave_type_id):
    with db.no_autoflush:
        rows=db.scalars(select(LeaveBalanceTransaction).where(
            LeaveBalanceTransaction.employee_id == employee_id,
            LeaveBalanceTransaction.leave_type_id == leave_type_id).order_by(LeaveBalanceTransaction.id))
        return exact_sum(ledger_amount(row) for row in rows)
