"""Current PostgreSQL global preparation only, no close or financial writes."""
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import select,func
from app.leave_month_close_readiness import prepare_month_close_readiness
from app.models import Employee,EmployeeServicePeriod,LeaveRegisterEntry,LeaveBalanceTransaction,LeaveMonthClose,PayrollRun,AuditLog
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_source_reconciliation import setup_source


def setup(history_context):
    db,e,args=setup_source(history_context)
    identity={k:args.pop(k) for k in ('actor_id','session_id','session_version','expected_epoch')}
    args['release_evaluated_on']=date(2026,2,28)
    return db,e,dict(year=2026,month=2,employee_selections={e.id:args},**identity)


def counts(db):
    return tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveRegisterEntry,LeaveBalanceTransaction,LeaveMonthClose,PayrollRun,AuditLog))


def test_complete_source_preparation_never_permission_or_backup(history_context):
    db,e,args=setup(history_context);before=counts(db)
    r=prepare_month_close_readiness(db,**args)
    row=next(x for x in r['rows'] if x['employee_id']==e.id)
    assert not row['issues'] and row['summary_digest'] and row['employed_days']==4
    assert not r['close_allowed'] and not r['backup_verified'] and not r['reconciliation_accepted']
    assert not r['payroll_money_allowed'] and not r['available_balance_established']
    assert counts(db)==before
    assert prepare_month_close_readiness(db,**args)==r


@pytest.mark.parametrize('change',['missing_selection','unknown','overlap','source_changed','wrongmonth','override','role','epoch','session'])
def test_current_source_and_authority_fail_closed(history_context,change):
    db,e,args=setup(history_context)
    first=prepare_month_close_readiness(db,**args)
    if change=='missing_selection':args['employee_selections']={}
    elif change=='unknown':db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id)).code=None
    elif change=='overlap':db.add(EmployeeServicePeriod(employee_id=e.id,start_date=date(2026,2,28),end_date=date(2026,2,28)))
    elif change=='source_changed':args['employee_selections'][e.id]['selection_ids']=('f'*64,)
    elif change=='wrongmonth':args['employee_selections'][e.id]['month_start']=date(2026,3,1)
    elif change=='override':args['employee_selections'][e.id]['actor_id']=args['actor_id']
    elif change=='role':history_context[1].role='finance'
    elif change=='epoch':args['expected_epoch']='stale'
    elif change=='session':args['session_id']='nonexistent'
    db.flush();before=counts(db)
    if change in {'wrongmonth','override','role','epoch','session'}:
        with pytest.raises(ValueError):prepare_month_close_readiness(db,**args)
    else:
        r=prepare_month_close_readiness(db,**args)
        assert not r['source_coverage_prepared'] and r['source_digest']!=first['source_digest']
        assert next(x for x in r['rows'] if x['employee_id']==e.id)['issues']
    assert counts(db)==before


def test_global_not_selected_department_and_missing_service_not_excluded(history_context):
    db,e,args=setup(history_context)
    other=Employee(code='SYN-GLOBAL',full_name='Synthetic unresolved employee',department_id=e.department_id,
        position='Synthetic',hire_date=date(2026,1,1),status='inactive',basic_salary=Decimal(1),daily_hours=Decimal(8))
    db.add(other);db.flush()
    r=prepare_month_close_readiness(db,**args)
    row=next(x for x in r['rows'] if x['employee_id']==other.id)
    assert row['service_missing'] and row['issues'] and not r['source_coverage_prepared']
    assert row['employed_days']==0  # unknown service, not fabricated employment.
    db.add(EmployeeServicePeriod(employee_id=other.id,start_date=date(2026,1,1),end_date=date(2026,1,31)));db.flush()
    ended=prepare_month_close_readiness(db,**args)
    assert other.id not in {x['employee_id'] for x in ended['rows']}
    db.add(LeaveRegisterEntry(employee_id=other.id,work_date=date(2026,2,2),code='A',source='synthetic',recorded_by='synthetic'));db.flush()
    conflict=prepare_month_close_readiness(db,**args)
    row=next(x for x in conflict['rows'] if x['employee_id']==other.id)
    assert row['conflicting_dates']==(date(2026,2,2),) and row['issues']


def test_overlapping_approved_payroll_blocks_without_mutation(history_context):
    db,e,args=setup(history_context)
    run=PayrollRun(period_start=date(2026,1,31),period_end=date(2026,2,1),pay_date=date(2026,2,3),status='approved',created_by='synthetic')
    db.add(run);db.flush();before=counts(db)
    r=prepare_month_close_readiness(db,**args)
    assert run.id in r['payroll_ids'] and 'approved_or_closed_payroll' in r['blockers']
    assert not r['source_coverage_prepared'] and counts(db)==before


@pytest.mark.skipif(__import__('os').environ.get('MANUAL_GLOBAL_READINESS_CONCURRENCY')!='1',reason='Standalone synthetic_cycle DB, commits fixture')
def test_actual_two_sessions_no_shared_to_exclusive_upgrade_deadlock(history_context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.db import SessionLocal
    db,e,args=setup(history_context)
    db.commit()
    barrier=Barrier(2)
    def read():
        with SessionLocal() as other:
            barrier.wait(timeout=5)
            r=prepare_month_close_readiness(other,**args)
            other.rollback()
            return r
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=[pool.submit(read) for _ in range(2)]
        rows=[j.result(timeout=12) for j in jobs]
    assert rows[0]==rows[1] and rows[0]['source_coverage_prepared']


def test_independent_close_lock_is_bounded_and_transaction_recoverable(history_context):
    import time
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from app.db import engine
    db,e,args=setup(history_context)
    with engine.connect() as blocker:
        tx=blocker.begin()
        try:
            blocker.execute(text("SET LOCAL lock_timeout = '1s'"))
            blocker.execute(text('LOCK TABLE leave_month_close IN ACCESS EXCLUSIVE MODE'))
            start=time.monotonic()
            with pytest.raises(OperationalError) as exc:
                with db.begin_nested():prepare_month_close_readiness(db,**args)
            assert getattr(exc.value.orig,'sqlstate',None)=='55P03'
            assert time.monotonic()-start<4.5
        finally:tx.rollback()
    assert prepare_month_close_readiness(db,**args)['rows']
