"""Real two-transaction committed ordering. Disposable migrated schema only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models import AdminAccount, AdminSession, Department, Employee, LeaveRecord, LeaveType, LeaveBalanceTransaction, AuditLog, PayrollRun
from app.leave_opening_apply import apply_confirmed_opening, review_opening_apply
from app.restore_lock import read_restore_epoch
from tests.test_leave_opening_apply import confirmed
from tests.test_leave_precision_migration import precision_schema, migrate  # noqa: F401


@pytest.fixture
def committed(precision_schema):  # noqa: F811
    e=precision_schema
    with e.begin() as c:migrate(c,'head')
    with Session(e) as db, db.begin():
        a=AdminAccount(username='synthetic-race-hr',password_hash='not-a-login',role='system_admin',active=True)
        db.add(a);db.flush();db.add(AdminSession(id='synthetic-race-session',admin_account_id=a.id));db.flush()
        auth=dict(actor_id=a.id,session_id='synthetic-race-session',session_version=a.session_version,expected_epoch=read_restore_epoch(db))
        d=Department(name='Synthetic race');db.add(d);db.flush()
        emp=Employee(code='SYN-RACE',full_name='Synthetic race',department_id=d.id,position='synthetic',hire_date=date(2025,1,1),status='active',basic_salary=1,daily_hours=8)
        db.add(emp);db.flush();eid=emp.id
        kinds={c:db.scalar(select(LeaveType.id).where(LeaveType.code==k)) for c,k in [('A','annual'),('W','weekly_rest'),('H','official_holiday')]}
        source=confirmed(db,SimpleNamespace(employee_id=eid),auth)
        r=review_opening_apply(db,selection_id=source.id,type_map=kinds,**auth)
        args=dict(selection_id=source.id,type_map=kinds,reviewed_apply_sha256=r['apply_sha256'],reason='Synthetic race',human_confirmed=True,**auth)
    return e,eid,kinds,args


def wait_blocked(engine,pid):
    end=monotonic()+4
    while monotonic()<end:
        with engine.connect() as c:
            if c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=:p AND wait_event_type='Lock')"),dict(p=pid)):return
        sleep(.01)
    raise AssertionError('Real competing backend never observed waiting for Lock')


def new_leave(db,eid,kind):
    db.add(LeaveRecord(employee_id=eid,leave_type_id=kind,start_date=date(2026,6,1),end_date=date(2026,6,1),calendar_days=1,charged_days=Decimal(1),reason='Synthetic race',status='draft',created_by='synthetic'))
    db.flush()


@pytest.mark.parametrize('change',['type','leave','payroll','payroll_audit'])
def test_contender_commits_first_apply_waits_then_refuses_no_writes(committed,change):
    e,eid,kinds,args=committed
    pid=[];started=Event()
    def apply():
        with Session(e) as db:
            pid.append(db.scalar(text('SELECT pg_backend_pid()')));started.set()
            try:
                apply_confirmed_opening(db,**args);db.commit()
            except ValueError:return 'refused'
            finally:db.rollback()
        return 'wrongly_applied'
    with Session(e) as first:
        if change=='type':first.execute(text('UPDATE leave_types SET active=false WHERE id=:i'),dict(i=kinds['W']))
        elif change=='leave':new_leave(first,eid,kinds['W'])
        else:
            first.add(PayrollRun(period_start=date(2026,5,1),period_end=date(2026,5,31),pay_date=date(2026,5,31),status='approved',created_by='synthetic'));first.flush()
            if change == 'payroll_audit':
                first.add(AuditLog(actor='synthetic',action='synthetic_payroll_audit',entity_type='payroll_run',entity_id='synthetic',reason='Synthetic'));first.flush()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result=pool.submit(apply)
            assert started.wait(3)
            wait_blocked(e,pid[0]);first.commit()
            assert result.result(timeout=5)=='refused'
    with e.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM leave_balance_transactions'))==0
        assert c.scalar(text("SELECT count(*) FROM audit_logs WHERE action='leave_opening_applied'"))==0


def test_apply_commits_first_other_leave_waits_then_commits(committed):
    e,eid,kinds,args=committed;pid=[];started=Event()
    def insert():
        with Session(e) as db:
            pid.append(db.scalar(text('SELECT pg_backend_pid()')));started.set()
            new_leave(db,eid,kinds['W']);db.commit();return 'committed'
    with Session(e) as first:
        receipt=apply_confirmed_opening(first,**args)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(insert);assert started.wait(3)
            wait_blocked(e,pid[0]);first.commit()
            assert pending.result(timeout=5)=='committed'
    with Session(e) as db:
        assert db.scalar(text('SELECT count(*) FROM leave_balance_transactions'))==2
        assert db.scalar(text('SELECT count(*) FROM leave_records'))==1
        assert apply_confirmed_opening(db,**args)['receipt_audit_id']==receipt['receipt_audit_id']


def test_identical_concurrent_apply_waits_then_returns_same_receipt(committed):
    e,eid,kinds,args=committed;pid=[];started=Event()
    def apply():
        with Session(e) as db:
            pid.append(db.scalar(text('SELECT pg_backend_pid()')));started.set()
            out=apply_confirmed_opening(db,**args);db.commit();return out
    with Session(e) as first:
        original=apply_confirmed_opening(first,**args)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(apply);assert started.wait(3)
            wait_blocked(e,pid[0]);first.commit()
            replay=pending.result(timeout=5)
    assert replay['replayed'] and replay['receipt_audit_id']==original['receipt_audit_id']
    with e.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM leave_balance_transactions'))==2
        assert c.scalar(text("SELECT count(*) FROM audit_logs WHERE action='leave_opening_applied'"))==1


def test_actual_lock_timeout_rolls_back_every_effect_and_retry_works(committed):
    e,eid,kinds,args=committed
    with Session(e) as blocker:
        blocker.execute(text('UPDATE leave_types SET active=false WHERE id=:i'),dict(i=kinds['W']))
        with Session(e) as db:
            db.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(DBAPIError,match='lock timeout'):apply_confirmed_opening(db,**args)
            db.rollback()
        blocker.rollback()
    with Session(e) as db:
        assert db.scalar(text('SELECT count(*) FROM leave_balance_transactions'))==0
        out=apply_confirmed_opening(db,**args);assert not out['replayed'];db.commit()


def test_actual_create_leave_draft_waits_then_completes(committed):
    from app.leave_workflow import create_leave_draft
    e,eid,kinds,args=committed;pid=[];started=Event()
    def create():
        with Session(e) as db:
            pid.append(db.scalar(text('SELECT pg_backend_pid()')));started.set()
            row=create_leave_draft(db,employee_id=eid,leave_type_id=kinds['W'],start_date=date(2026,6,2),end_date=date(2026,6,2),charged_days=Decimal(1),reason='Synthetic',actor='synthetic')
            db.commit();return row.id
    with Session(e) as first:
        apply_confirmed_opening(first,**args)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(create);assert started.wait(3)
            wait_blocked(e,pid[0]);first.commit();assert pending.result(timeout=5)>0
