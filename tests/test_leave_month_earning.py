"""Migrated actual source -> exact earned/usage/release -> authoritative readers."""
from datetime import date,timedelta
from decimal import Decimal
from fractions import Fraction
import json
import pytest
from sqlalchemy import select,text,func
from sqlalchemy.exc import DBAPIError
from app.leave_month_earning import review_month_earning,apply_month_earning
from app.leave_opening_apply import review_opening_apply,apply_confirmed_opening
from app.leave_policy_preview import LeavePolicy
from app.leave_policy_store import persist_policy_version
from app.leave_manual_opening_groups import persist_manual_opening_group
from app.leave_opening_source import review_opening_source,confirm_opening_source
from app.leave_workflow import leave_balance
from app.models import (Employee,EmployeeServicePeriod,LeaveRegisterEntry,LeaveMonthEarning,
    LeaveType,LeaveBalanceTransaction,AuditLog)
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa


@pytest.fixture
def monthly(history_context):
    db,_actor,auth,fact=history_context
    employee=db.get(Employee,fact.employee_id);employee.hire_date=date(2026,2,25)
    db.add(EmployeeServicePeriod(employee_id=employee.id,start_date=employee.hire_date))
    policy=persist_policy_version(db,policy=LeavePolicy(False,False,True,False,'calendar','prorated',False,(90,180)),reason='Synthetic explicit',**auth)
    group,_=persist_manual_opening_group(db,employee_id=employee.id,balances={c:Decimal(0) for c in ('A','W','H')},effective_start=employee.hire_date,reason='Synthetic baseline',**auth)
    review=review_opening_source(db,group_id=group.id,policy_id=policy.id,**auth)
    selected=confirm_opening_source(db,group_id=group.id,policy_id=policy.id,expected_previous_id=None,reviewed_source_sha256=review['source_sha256'],reason='Synthetic confirm',human_confirmed=True,**auth)
    kinds={c:db.scalar(select(LeaveType.id).where(LeaveType.code==host)) for c,host in {'A':'annual','W':'weekly_rest','H':'official_holiday'}.items()}
    opening=review_opening_apply(db,selection_id=selected.id,type_map=kinds,**auth)
    apply_confirmed_opening(db,selection_id=selected.id,type_map=kinds,reviewed_apply_sha256=opening['apply_sha256'],human_confirmed=True,reason='Synthetic opening',**auth)
    for n in range(35):
        day=employee.hire_date+timedelta(days=n)
        db.add(LeaveRegisterEntry(employee_id=employee.id,work_date=day,code='A' if day==date(2026,2,28) else '',source='reviewed-return',recorded_by='synthetic'))
    db.flush()
    def args(month,evaluated):
        months=[date(2026,m,1) for m in range(1,month.month+1)]
        return dict(selection_id=selected.id,month_start=month,evaluated_on=evaluated,
            current_insurance_years={(employee.id,m):None for m in months},age_evaluation_dates={m:m for m in months},**auth)
    yield db,employee,kinds,args


def post(db,args,operation='a'*64):
    review=review_month_earning(db,**args)
    result=apply_month_earning(db,operation_id=operation,reviewed_digest=review['reviewed_digest'],human_confirmed=True,reason='Synthetic reviewed earning',**args)
    return review,result


def test_partial_hire_month_exact_held_and_usage_not_assumed_debited(monthly):
    db,e,kinds,args=monthly
    selection=args(date(2026,2,1),date(2026,2,28))
    review,result=post(db,selection)
    assert result['held_annual']=='5/28'
    assert leave_balance(db,e.id,kinds['A'])==Decimal(-1)  # explicit A use, annual held
    assert leave_balance(db,e.id,kinds['W'])==Fraction(4,7)
    assert leave_balance(db,e.id,kinds['H'])==Decimal(0)
    assert db.scalar(select(func.count()).select_from(LeaveMonthEarning))==1
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    assert apply_month_earning(db,operation_id='a'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,reason='Synthetic reviewed earning',**selection)==result
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before


def test_next_full_month_releases_previous_annual_once_and_mixed_readers(monthly):
    db,e,kinds,args=monthly
    post(db,args(date(2026,2,1),date(2026,2,28)))
    review,result=post(db,args(date(2026,3,1),date(2026,4,1)),'b'*64)
    assert result['held_annual']=='0/1'
    assert {r['earning_month'] for r in result['ledger'] if r['category']=='A'}=={'2026-02-01','2026-03-01'}
    assert leave_balance(db,e.id,kinds['A'])==Fraction(3,7)  # 5/28+5/4-1
    assert leave_balance(db,e.id,kinds['W'])==Decimal(5)
    from app.leave_exact_read import exact_ledger_balance
    assert exact_ledger_balance(db,e.id,kinds['A'])==Fraction(3,7)
    with pytest.raises(ValueError,match='already posted'):
        apply_month_earning(db,operation_id='c'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,reason='Synthetic reviewed earning',**args(date(2026,3,1),date(2026,4,1)))


def test_stale_register_refuses_without_partial_post(monthly):
    db,e,kinds,args=monthly
    selection=args(date(2026,2,1),date(2026,2,28));review=review_month_earning(db,**selection)
    entry=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,25)))
    entry.code='U';db.flush()
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    with pytest.raises(ValueError,match='changed'):
        apply_month_earning(db,operation_id='a'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,reason='Synthetic',**selection)
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before
    assert db.scalar(select(func.count()).select_from(LeaveMonthEarning))==0


def test_prior_held_source_change_blocks_retroactive_release(monthly):
    db,e,kinds,args=monthly
    post(db,args(date(2026,2,1),date(2026,2,28)))
    entry=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,25)))
    entry.code='U';db.flush()
    with pytest.raises(ValueError,match='Prior earned source changed'):
        review_month_earning(db,**args(date(2026,3,1),date(2026,4,1)))


def test_unknown_source_and_skipped_month_refuse(monthly):
    db,e,kinds,args=monthly
    with pytest.raises(ValueError,match='sequentially'):
        review_month_earning(db,**args(date(2026,3,1),date(2026,4,1)))
    entry=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,25)))
    entry.code=None;db.flush()
    with pytest.raises(ValueError,match='Complete explicit register'):
        review_month_earning(db,**args(date(2026,2,1),date(2026,2,28)))


@pytest.mark.parametrize('command',['UPDATE leave_month_earnings SET source_digest=source_digest','DELETE FROM leave_month_earnings','TRUNCATE leave_month_earnings'])
def test_receipt_immutable_all_dml(monthly,command):
    db,e,kinds,args=monthly;post(db,args(date(2026,2,1),date(2026,2,28)))
    with pytest.raises(DBAPIError,match='immutable'),db.begin_nested():db.execute(text(command))


def test_held_full_month_can_release_next_first_without_waiting_another_month(monthly):
    from app.leave_month_earning import review_annual_release,apply_annual_release
    db,e,kinds,args=monthly
    post(db,args(date(2026,2,1),date(2026,2,28)))
    post(db,args(date(2026,3,1),date(2026,3,31)),'b'*64)
    assert leave_balance(db,e.id,kinds['A'])==Decimal(-1)
    selection=args(date(2026,3,1),date(2026,4,1))
    review=review_annual_release(db,**selection)
    result=apply_annual_release(db,operation_id='d'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,reason='Synthetic release',**selection)
    assert len(result['ledger'])==2 and leave_balance(db,e.id,kinds['A'])==Fraction(3,7)
    assert apply_annual_release(db,operation_id='d'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,reason='Synthetic release',**selection)==result
    with pytest.raises(ValueError,match='No unreleased'):
        review_annual_release(db,**selection)


def test_midmonth_baseline_cutoff_and_unpaid_sick_policy_are_exact(monthly):
    db,e,kinds,args=monthly
    entry=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,25)))
    entry.code='U'
    sick=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,26)))
    sick.code='S';db.flush()
    review,result=post(db,args(date(2026,2,1),date(2026,2,28)))
    assert result['held_annual']=='15/112'  # U deducted, S not deducted by explicit policy
    assert leave_balance(db,e.id,kinds['W'])==Fraction(2,7)


def test_closed_month_stale_authority_and_future_evaluation_refuse(monthly):
    db,e,kinds,args=monthly
    selection=args(date(2026,2,1),date(2026,2,28))
    with pytest.raises(ValueError,match='authorized'):
        review_month_earning(db,**(selection|{'session_version':selection['session_version']+1}))
    with pytest.raises(ValueError,match='current evaluation'):
        review_month_earning(db,**args(date(2026,2,1),date(9999,1,1)))
    from app.models import LeaveMonthClose,BackupRun,utcnow
    backup=BackupRun(backup_type='manual',status='success',archive_path='synthetic-only',size_bytes=1,initiated_by='synthetic')
    # Field names are from the actual model below; this is a source-only fixture.
    db.add(backup);db.flush()
    db.add(LeaveMonthClose(year=2026,month=2,backup_run_id=backup.id,closed_by='synthetic',closed_at=utcnow(),policy_snapshot_json='{}'))
    db.flush()
    with pytest.raises(ValueError,match='Closed leave month'):
        review_month_earning(db,**selection)


def test_arbitrary_unrelated_ledger_not_assumed_reconciled(monthly):
    db,e,kinds,args=monthly
    db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=kinds['A'],transaction_type='adjustment',days_delta=Decimal(2),reason='Synthetic unrelated',created_by='synthetic'))
    db.flush()
    with pytest.raises(ValueError,match='unrelated ledger'):
        review_month_earning(db,**args(date(2026,2,1),date(2026,2,28)))
