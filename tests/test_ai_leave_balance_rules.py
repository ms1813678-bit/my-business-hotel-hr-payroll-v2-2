from datetime import date
from decimal import Decimal, localcontext

import pytest
from sqlalchemy import func, select

from app.ai_agent import execute_plan
from app.ai_gateway import build_confirmation_preview
from app.ai_hr_queries import format_leave_balance
from app.db import SessionLocal
from app.leave_balance_rules import preview_manual_balance, validate_manual_balance_change
from app.leave_workflow import add_balance_entry, leave_balance
from app.models import Department, Employee, LeaveBalanceTransaction, LeaveType


@pytest.mark.parametrize('kind,amount',[('opening','-2'),('grant','2'),('settlement','-2'),('leave_use','-2'),('adjustment','0'),('adjustment','NaN')])
def test_manual_ai_boundary_cannot_choose_unimplemented_effects(kind,amount):
    with pytest.raises(ValueError):validate_manual_balance_change(kind,Decimal(amount))


def test_shared_preview_does_not_depend_on_ambient_precision():
    with localcontext() as ctx:
        ctx.prec=3
        value=preview_manual_balance(Decimal(1), 'adjustment',Decimal('-0.000000000000000000000000000001'))
    assert str(value)=='0.999999999999999999999999999999'


def test_real_pg_signed_tiny_adjustment_preview_execution_report_and_stale_guard():
    with SessionLocal() as db:
        try:
            dep=Department(name='Synthetic AI exact manual',active=True);db.add(dep);db.flush()
            e=Employee(code='SYN-AI-EXACT',full_name='Synthetic only',department_id=dep.id,
                position='Synthetic',hire_date=date(2026,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8))
            t=LeaveType(code='syn_ai_exact',name_ar='Synthetic',name_en='Synthetic',tracks_balance=True,active=True,is_paid=True)
            db.add_all([e,t]);db.flush()
            add_balance_entry(db,employee_id=e.id,leave_type=t,transaction_type='opening',days_delta=Decimal(1),reason='Synthetic opening',actor='synthetic')
            delta=Decimal('-1.000000000000000000000000000001')
            plan={'action':'adjust_leave_balance','employee_id':e.id,'leave_type_id':t.id,
                'balance_days':str(delta),'transaction_type':'adjustment','reason':'Synthetic reviewed manual correction'}
            preview=build_confirmation_preview(plan,session=db)
            assert Decimal(preview['new_values']['resulting_balance'])==Decimal('-0.000000000000000000000000000001')
            plan['_confirmation_snapshot']=preview['snapshot']
            execute_plan(plan,'synthetic',db=db)
            result=leave_balance(db,e.id,t.id)
            assert result==Decimal('-0.000000000000000000000000000001')
            report={'note':'note_readonly_leave','employees':[{'code':e.code,'name':e.full_name,'balances':[{'leave_type_ar':'Synthetic','leave_type_en':'Synthetic','leave_type_zh':'Synthetic','days':str(result)}]}]}
            for lang in ('ar','en','zh'):assert str(result) in format_leave_balance(report,lang)
            count=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id==e.id))
            with pytest.raises(ValueError,match='تغير رصيد'):
                execute_plan(plan,'synthetic',db=db)
            assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id==e.id))==count
            for kind in ('opening','grant','settlement','leave_use'):
                bad={**plan,'transaction_type':kind};bad.pop('_confirmation_snapshot',None)
                with pytest.raises(ValueError):build_confirmation_preview(bad,session=db)
                with pytest.raises(ValueError):execute_plan(bad,'synthetic',db=db)
            assert leave_balance(db,e.id,t.id)==result
        finally:db.rollback()


@pytest.mark.parametrize('kind',[[],{}])
def test_invalid_kind_is_explicit_refusal(kind):
    with pytest.raises(ValueError):validate_manual_balance_change(kind,Decimal(1))


def test_context_exponent_and_traps_cannot_change_exact_result():
    from decimal import Inexact, Overflow, Underflow
    for trapped in (True,False):
        with localcontext() as outer:
            outer.Emax=9;outer.Emin=-9
            outer.traps[Overflow]=trapped;outer.traps[Underflow]=trapped;outer.traps[Inexact]=trapped
            assert preview_manual_balance(Decimal('1e10'),'adjustment',Decimal(1))==Decimal(10000000001)
            assert preview_manual_balance(Decimal('1e-100'),'adjustment',Decimal('1e-100'))==Decimal('2e-100')


@pytest.mark.parametrize('value',['1e1000000','1e-1000050','1e100000'])
def test_extreme_resource_inputs_refused_not_rounded(value):
    with pytest.raises(ValueError,match='bound'):
        preview_manual_balance(Decimal(1),'adjustment',Decimal(value))


def test_manual_writer_refuses_preview_span_before_ledger_or_audit_mutation():
    from app.models import AuditLog
    with SessionLocal() as db:
        try:
            dep=Department(name='Synthetic bound refusal',active=True);db.add(dep);db.flush()
            e=Employee(code='SYN-BOUND',full_name='Synthetic',department_id=dep.id,position='Synthetic',hire_date=date(2026,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8))
            t=LeaveType(code='syn_bound',name_ar='Synthetic',name_en='Synthetic',tracks_balance=True,active=True,is_paid=True)
            db.add_all([e,t]);db.flush()
            # Existing legacy source can sit at a boundary; do not rewrite it.
            db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=t.id,transaction_type='adjustment',days_delta=Decimal('1e4096'),reason='Synthetic source',created_by='synthetic'));db.flush()
            ledger_before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
            audit_before=db.scalar(select(func.count()).select_from(AuditLog))
            with pytest.raises(ValueError,match='bound'):
                add_balance_entry(db,employee_id=e.id,leave_type=t,transaction_type='adjustment',days_delta=Decimal(1),reason='Synthetic refusal',actor='synthetic')
            assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==ledger_before
            assert db.scalar(select(func.count()).select_from(AuditLog))==audit_before
        finally:db.rollback()
