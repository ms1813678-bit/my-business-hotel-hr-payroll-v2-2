"""Exact existing-ledger reads for the new leave integration only.

No second ledger, no quantizing, no balance posting or payroll mutation. The
old leave_workflow helpers and routes retain their historic behavior until
explicitly integrated. This total is ledger truth, not a new-hire release gate.
"""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Employee, LeaveBalanceTransaction, LeaveType


def exact_ledger_balance(db: Session, employee_id: int, leave_type_id: int) -> Decimal:
    for value in (employee_id, leave_type_id):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError('Resolved positive employee/type IDs required.')
    with db.no_autoflush:
        if db.get(Employee, employee_id) is None or db.get(LeaveType, leave_type_id) is None:
            raise ValueError('Employee/type evidence does not exist; no zero balance inferred.')
        value = db.scalar(select(func.coalesce(func.sum(LeaveBalanceTransaction.days_delta), 0)).where(
            LeaveBalanceTransaction.employee_id == employee_id,
            LeaveBalanceTransaction.leave_type_id == leave_type_id))
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError('Ledger contains nonfinite day evidence.')
    return result
