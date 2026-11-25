"""Actual configured migrated PostgreSQL, no create_all substitute."""
from datetime import date,datetime,UTC
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Department,Employee,PayrollRun,PayrollItem,PayrollPayment
from app.payroll_workflow import update_payroll_item,approve_overtime

@pytest.fixture
def actual_item():
    with SessionLocal() as db:
        if db.get_bind().dialect.name!='postgresql':pytest.skip('Migrated PostgreSQL required')
        dep=Department(name='Synthetic OT basis',active=True);db.add(dep);db.flush()
        emp=Employee(code='MY-TEST-OT-BASIS',full_name='Synthetic OT',department_id=dep.id,position='Synthetic',hire_date=date(2026,1,1),status='active',basic_salary=Decimal('6000'),daily_hours=Decimal('8'));db.add(emp);db.flush()
        run=PayrollRun(period_start=date(2026,10,1),period_end=date(2026,10,31),pay_date=date(2026,11,5),status='draft',created_by='synthetic');db.add(run);db.flush()
        item=PayrollItem(payroll_run_id=run.id,employee_id=emp.id,employee_code_snapshot=emp.code,employee_name_snapshot=emp.full_name,department_name_snapshot=dep.name,position_snapshot=emp.position,salary_basis=Decimal('6000'),base_salary=Decimal('6000'),daily_hours=Decimal('8'))
        db.add(item);db.flush()
        try:yield db,item,run
        finally:db.rollback()

def test_actual_basis_change_invalidates_and_reapproval_recomputes(actual_item):
    db,item,run=actual_item
    update_payroll_item(db,item,{'overtime_hours':2,'performance_bonus':1200},'synthetic')
    assert not item.overtime_approved and item.overtime_amount==0
    approve_overtime(db,item,'synthetic');assert item.overtime_amount==Decimal('60.00')
    update_payroll_item(db,item,{'performance_bonus':2400},'synthetic')
    assert not item.overtime_approved and item.overtime_amount==0 and item.overtime_approved_by is None
    approve_overtime(db,item,'synthetic');assert item.overtime_amount==Decimal('70.00')
    update_payroll_item(db,item,{'performance_bonus':2400,'other_bonus':999},'synthetic')
    assert item.overtime_approved and item.overtime_amount==Decimal('70.00')

@pytest.mark.parametrize('status',['approved','closed'])
def test_actual_closed_approved_guard_preserved(actual_item,status):
    db,item,run=actual_item;run.status=status
    with pytest.raises(ValueError,match='draft or reopened'):update_payroll_item(db,item,{'performance_bonus':1200},'synthetic')
    assert item.performance_bonus==0


def test_actual_paid_item_edit_refuses_before_basis_change(actual_item):
    db,item,run=actual_item
    payment=PayrollPayment(payroll_item_id=item.id,amount=Decimal('1'),paid_at=datetime.now(UTC),method='cash',created_by='synthetic')
    db.add(payment);db.flush()
    with pytest.raises(ValueError,match='recorded payments'):
        update_payroll_item(db,item,{'performance_bonus':1200},'synthetic')
    assert item.performance_bonus==0 and not item.overtime_approved
