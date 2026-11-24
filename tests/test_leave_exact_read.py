from datetime import date
from decimal import Decimal

from sqlalchemy import select, text

from app.db import SessionLocal
from app.leave_exact_read import exact_ledger_balance
from app.models import Department, Employee, LeaveBalanceTransaction, LeaveType


def test_exact_existing_ledger_roundtrip_and_read_has_no_unrelated_flush():
    with SessionLocal() as db:
        try:
            department = Department(name='Synthetic exact ledger', active=True)
            db.add(department)
            db.flush()
            employee = Employee(code='SYN-EXACT-44', full_name='Synthetic only',
                department_id=department.id, position='Synthetic', hire_date=date(2026, 1, 1),
                status='active', basic_salary=Decimal(1), daily_hours=Decimal(8))
            leave_type = LeaveType(code='syn44', name_ar='Synthetic', name_en='Synthetic',
                                   tracks_balance=True, active=True, is_paid=True)
            db.add_all([employee, leave_type])
            db.flush()
            amount = Decimal('0.14285714285714285714285714285714285714285714285714')
            db.add(LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=leave_type.id,
                transaction_type='adjustment', days_delta=amount, reason='Synthetic exact probe',
                created_by='synthetic'))
            db.flush()
            db.expire_all()
            assert exact_ledger_balance(db, employee.id, leave_type.id) == amount
            db.add(LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=leave_type.id,
                transaction_type='adjustment', days_delta=Decimal(-2), reason='Synthetic negative',
                created_by='synthetic'))
            db.flush()
            value = exact_ledger_balance(db, employee.id, leave_type.id)
            assert str(value) == '-1.85714285714285714285714285714285714285714285714286'
            employee.basic_salary = Decimal(999)
            exact_ledger_balance(db, employee.id, leave_type.id)
            assert employee in db.dirty
            assert db.execute(text('select basic_salary from employees where id=:id'),
                              {'id': employee.id}).scalar_one() == Decimal(1)
            assert db.scalar(select(LeaveBalanceTransaction.days_delta).where(
                LeaveBalanceTransaction.employee_id == employee.id).order_by(
                LeaveBalanceTransaction.id)) == amount
        finally:
            db.rollback()


def test_missing_identity_never_becomes_plausible_zero_balance():
    import pytest
    with SessionLocal() as db, pytest.raises(ValueError, match='does not exist'):
        exact_ledger_balance(db, 2147483647, 2147483647)


def test_existing_identity_with_no_ledger_rows_is_exact_zero_without_flush():
    with SessionLocal() as db:
        try:
            department = Department(name='Synthetic zero ledger', active=True)
            db.add(department)
            db.flush()
            employee = Employee(code='SYN-ZERO-44', full_name='Synthetic zero',
                department_id=department.id, position='Synthetic', hire_date=date(2026, 1, 1),
                status='active', basic_salary=Decimal(1), daily_hours=Decimal(8))
            leave_type = LeaveType(code='syn44zero', name_ar='Synthetic', name_en='Synthetic',
                                   tracks_balance=True, active=True, is_paid=True)
            db.add_all([employee, leave_type])
            db.flush()
            pending = LeaveBalanceTransaction(employee_id=employee.id,
                leave_type_id=leave_type.id, transaction_type='adjustment',
                days_delta=Decimal('0.33333333333333333333333333333333333333333333333333'),
                reason='Synthetic pending only', created_by='synthetic')
            db.add(pending)
            assert exact_ledger_balance(db, employee.id, leave_type.id) == Decimal(0)
            assert pending in db.new
            assert pending.id is None
            db.flush()
            assert exact_ledger_balance(db, employee.id, leave_type.id) == pending.days_delta
        finally:
            db.rollback()


def test_invalid_identity_values_never_query_or_flush():
    import pytest

    class NoDatabaseAccess:
        def __getattr__(self, name):
            pytest.fail('Invalid identity must be rejected before database access.')

    for invalid in (True, False, 0, -1, '1', Decimal(1), None):
        for employee_id, leave_type_id in ((invalid, 1), (1, invalid)):
            with pytest.raises(ValueError, match='Resolved positive'):
                exact_ledger_balance(NoDatabaseAccess(), employee_id, leave_type_id)


def test_nonfinite_legacy_ledger_evidence_never_becomes_a_balance():
    import pytest

    with SessionLocal() as db:
        try:
            department = Department(name='Synthetic nonfinite ledger', active=True)
            db.add(department)
            db.flush()
            employee = Employee(code='SYN-NONFINITE-44', full_name='Synthetic nonfinite',
                department_id=department.id, position='Synthetic', hire_date=date(2026, 1, 1),
                status='active', basic_salary=Decimal(1), daily_hours=Decimal(8))
            leave_type = LeaveType(code='syn44nonfinite', name_ar='Synthetic', name_en='Synthetic',
                                   tracks_balance=True, active=True, is_paid=True)
            db.add_all([employee, leave_type])
            db.flush()
            for value in ('NaN', 'Infinity', '-Infinity'):
                with db.begin_nested():
                    entry = LeaveBalanceTransaction(employee_id=employee.id,
                        leave_type_id=leave_type.id, transaction_type='adjustment',
                        days_delta=Decimal(value), reason='Synthetic invalid evidence',
                        created_by='synthetic')
                    db.add(entry)
                    db.flush()
                    with pytest.raises(ValueError, match='nonfinite'):
                        exact_ledger_balance(db, employee.id, leave_type.id)
                    db.delete(entry)
                    db.flush()
        finally:
            db.rollback()
