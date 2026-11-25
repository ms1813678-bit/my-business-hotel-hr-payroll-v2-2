"""Actual migrated PG ledger amounts, no monthly authority implied by fixtures."""
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from app.db import SessionLocal
from app.models import Department, Employee, LeaveType, LeaveBalanceTransaction
from app.leave_exact_read import exact_ledger_balance
from app.leave_ledger_read import grouped_ledger_balances
from app.leave_workflow import leave_balance, add_balance_entry
from app.leave_exact_amount import exact_sum, exact_text


@pytest.fixture
def rational_store():
    with SessionLocal() as db:
        department=Department(name='Synthetic rational 1765',active=True)
        db.add(department);db.flush()
        employee=Employee(code='SYN-RAT-1765',full_name='Synthetic rational only',
            department_id=department.id,position='Synthetic',hire_date=date(2026,1,1),
            status='active',basic_salary=Decimal(1),daily_hours=Decimal(8))
        kind=LeaveType(code='syn-rat-1765',name_ar='Synthetic',name_en='Synthetic',
            tracks_balance=True,active=True,is_paid=True)
        db.add_all([employee,kind]);db.flush()
        yield db,employee,kind
        db.rollback()


def row(employee,kind,**changes):
    return LeaveBalanceTransaction(employee_id=employee.id,leave_type_id=kind.id,
        transaction_type='adjustment',reason='Synthetic exact row',created_by='synthetic',**changes)


def test_seven_repeating_credits_and_finite_manual_adjustment_all_readers(rational_store):
    db,employee,kind=rational_store
    db.add_all([row(employee,kind,days_delta=None,exact_numerator=Decimal(1),exact_denominator=Decimal(7)) for _ in range(7)])
    db.flush()
    with localcontext() as context:
        context.prec=2
        assert exact_ledger_balance(db,employee.id,kind.id)==Decimal(1)
        assert leave_balance(db,employee.id,kind.id)==Decimal(1)
        delta=Decimal('-1.123456789123456789123456789123456789')
        add_balance_entry(db,employee_id=employee.id,leave_type=kind,transaction_type='adjustment',
            days_delta=delta,reason='Synthetic exact manual',actor='synthetic')
        expected=Decimal('-.123456789123456789123456789123456789')
        assert leave_balance(db,employee.id,kind.id)==expected
        assert grouped_ledger_balances(db)[(employee.id,kind.id)]==expected
    # The old NULL-skipping SUM demonstrably is not an admissible balance read.
    assert db.scalar(text('select sum(days_delta) from leave_balance_transactions where employee_id=:e'),{'e':employee.id})==delta


def test_one_seventh_manual_preview_audit_and_negative_result(rational_store):
    db,employee,kind=rational_store
    db.add(row(employee,kind,days_delta=None,exact_numerator=Decimal(1),exact_denominator=Decimal(7)))
    db.flush()
    from app.leave_balance_rules import preview_manual_balance
    assert preview_manual_balance(leave_balance(db,employee.id,kind.id),'adjustment',Decimal('-.2'))==Fraction(-2,35)
    add_balance_entry(db,employee_id=employee.id,leave_type=kind,transaction_type='adjustment',
        days_delta=Decimal('-.2'),reason='Synthetic exact manual',actor='synthetic')
    assert exact_text(leave_balance(db,employee.id,kind.id))=='-2/35'
    from app.models import AuditLog
    db.flush()
    audit=db.scalar(select(AuditLog).where(AuditLog.entity_id==f'{employee.id}:{kind.id}').order_by(AuditLog.id.desc()))
    assert audit.old_value=='1/7' and audit.new_value=='-2/35'


@pytest.mark.parametrize('changes',[
    dict(days_delta=None),dict(days_delta=Decimal(1),exact_numerator=Decimal(1),exact_denominator=Decimal(7)),
    dict(days_delta=None,exact_numerator=Decimal(1)),
    dict(days_delta=None,exact_numerator=Decimal(0),exact_denominator=Decimal(7)),
    dict(days_delta=None,exact_numerator=Decimal(1),exact_denominator=Decimal(0)),
    dict(days_delta=None,exact_numerator=Decimal(1),exact_denominator=Decimal(-7)),
    dict(days_delta=None,exact_numerator=Decimal('1.5'),exact_denominator=Decimal(7)),
    dict(days_delta=None,exact_numerator=Decimal(2),exact_denominator=Decimal(14)),
    dict(days_delta=None,exact_numerator=Decimal('NaN'),exact_denominator=Decimal(7)),
    dict(days_delta=None,exact_numerator=Decimal(1),exact_denominator=Decimal('Infinity')),
])
def test_database_refuses_invalid_exclusive_exact_representations(rational_store,changes):
    db,employee,kind=rational_store
    with pytest.raises(IntegrityError),db.begin_nested():
        db.add(row(employee,kind,**changes));db.flush()


def test_employee_cross_type_semantics_include_nontracking_types(rational_store):
    db,employee,kind=rational_store
    other=LeaveType(code='syn-rat-other',name_ar='Synthetic other',name_en='Synthetic other',
        tracks_balance=False,active=True,is_paid=True)
    db.add(other);db.flush()
    db.add(row(employee,kind,days_delta=None,exact_numerator=Decimal(1),exact_denominator=Decimal(7)))
    db.add(row(employee,other,days_delta=Decimal('2.50')));db.flush()
    totals=grouped_ledger_balances(db,employee_ids=[employee.id])
    assert exact_sum(totals.values())==Fraction(37,14)


def test_business_code_does_not_sum_nullable_finite_only_column():
    from pathlib import Path
    import re
    root=Path(__file__).resolve().parents[1]/'app'
    rejected=[]
    for path in root.rglob('*.py'):
        source=path.read_text(encoding='utf-8')
        if re.search(r'func\.sum\(\s*LeaveBalanceTransaction\.days_delta',source): rejected.append(path.name)
    assert not rejected
