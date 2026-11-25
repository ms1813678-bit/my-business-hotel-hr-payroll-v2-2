"""Exact existing-ledger reads for the new leave integration only.

No second ledger, no quantizing, no balance posting or payroll mutation. All production balance readers share the mixed exact amount adapter. This total is ledger truth, not a new-hire release gate.
"""
from decimal import Decimal
from fractions import Fraction

from sqlalchemy.orm import Session

from app.models import Employee, LeaveType


def exact_ledger_balance(db: Session, employee_id: int, leave_type_id: int) -> Decimal | Fraction:
    for value in (employee_id, leave_type_id):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError('Resolved positive employee/type IDs required.')
    with db.no_autoflush:
        if db.get(Employee, employee_id) is None or db.get(LeaveType, leave_type_id) is None:
            raise ValueError('Employee/type evidence does not exist; no zero balance inferred.')
        from app.leave_ledger_read import ledger_balance
        return ledger_balance(db, employee_id, leave_type_id)
