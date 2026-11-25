"""True concurrent PostgreSQL transactions in an owned, migrated throwaway schema."""
from datetime import date,datetime
from decimal import Decimal
import threading
from uuid import uuid4
import pytest
from sqlalchemy import create_engine,select,text,func
from sqlalchemy.orm import sessionmaker
from alembic import command
from alembic.config import Config
from app.db import engine
from app.models import AdminAccount,AdminSession,Department,Employee,PayrollRun,PayrollItem,AttendanceDailyRecord,OvertimeDayAward
from app.overtime_day_award import prepare_day_award,confirm_day_award

@pytest.fixture
def committed_day():
    if engine.dialect.name!='postgresql':pytest.skip('Actual migrated PostgreSQL required')
    schema='synthetic_ot_'+uuid4().hex
    with engine.begin() as conn:conn.execute(text('CREATE SCHEMA '+schema))
    local=create_engine(engine.url,connect_args={'options':'-csearch_path='+schema},pool_size=4)
    try:
        with local.begin() as conn:
            cfg=Config('alembic.ini');cfg.attributes['connection']=conn;command.upgrade(cfg,'head')
        Session=sessionmaker(local,autoflush=False)
        with Session.begin() as db:
            admin=AdminAccount(username='synthetic-ot-race',password_hash='not-real',role='system_admin',active=True);db.add(admin);db.flush()
            login=AdminSession(id='synthetic-ot-race-session',admin_account_id=admin.id);db.add(login)
            dept=Department(name='Synthetic OT race',active=True);db.add(dept);db.flush()
            emp=Employee(code='SYN-OT-RACE',full_name='Synthetic OT',department_id=dept.id,position='Synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(6000),daily_hours=Decimal(8));db.add(emp);db.flush()
            run=PayrollRun(period_start=date(2026,2,1),period_end=date(2026,2,28),pay_date=date(2026,3,5),created_by='synthetic',status='draft');db.add(run);db.flush()
            item=PayrollItem(payroll_run_id=run.id,employee_id=emp.id,employee_code_snapshot=emp.code,employee_name_snapshot=emp.full_name,department_name_snapshot=dept.name,position_snapshot=emp.position,salary_basis=Decimal(6000),base_salary=Decimal(6000),daily_hours=Decimal(8));db.add(item);db.flush()
            a=AttendanceDailyRecord(employee_id=emp.id,work_date=date(2026,2,2),status='approved',scheduled_start_at=datetime(2026,2,2,9),scheduled_end_at=datetime(2026,2,2,17),approved_clock_in=datetime(2026,2,2,9),approved_clock_out=datetime(2026,2,2,19),worked_minutes=600);db.add(a);db.flush()
            auth=dict(actor_id=admin.id,session_id=login.id,session_version=admin.session_version,expected_epoch=None)
            item_id=item.id;attendance_id=a.id
        yield Session,auth,item_id,attendance_id
    finally:
        local.dispose()
        with engine.begin() as conn:conn.execute(text('DROP SCHEMA '+schema+' CASCADE'))

def prepared(Session,auth,item_id,op):
    with Session.begin() as db:
        return prepare_day_award(db,operation_id=op*64,item_id=item_id,work_date=date(2026,2,2),manual_hours=Decimal(2),rate_divisor=8,reason='',**auth).id

@pytest.mark.parametrize('same_receipt',[True,False])
def test_parallel_confirm_once_no_deadlock(committed_day,same_receipt):
    Session,auth,item_id,attendance_id=committed_day
    first=prepared(Session,auth,item_id,'a');second=first if same_receipt else prepared(Session,auth,item_id,'b')
    barrier=threading.Barrier(2);out=[]
    def run(receipt):
        try:
            with Session.begin() as db:
                barrier.wait(timeout=5);award=confirm_day_award(db,receipt_id=receipt,human_confirmed=True,**auth);out.append(('ok',award.receipt_id))
        except Exception as exc:out.append((type(exc).__name__,str(exc)))
    workers=[threading.Thread(target=run,args=(r,)) for r in (first,second)]
    for worker in workers:worker.start()
    for worker in workers:worker.join(10)
    assert not any(w.is_alive() for w in workers)
    assert len(out)==2
    if same_receipt:assert out==[('ok',first),('ok',first)]
    else:assert sorted(x[0] for x in out)==['ValueError','ok']
    with Session() as db:assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==1


def test_source_edit_waits_for_confirmation_then_stales(committed_day):
    from app.overtime_day_award import verified_day_totals
    Session,auth,item_id,attendance_id=committed_day;receipt=prepared(Session,auth,item_id,'a')
    started=threading.Event();done=threading.Event();errors=[]
    def edit():
        try:
            with Session.begin() as db:
                started.set();db.execute(text('UPDATE attendance_daily_records SET review_notes=:note WHERE id=:id'),dict(note='Synthetic changed after confirmation',id=attendance_id))
            done.set()
        except Exception as exc:errors.append(exc)
    with Session.begin() as db:
        confirm_day_award(db,receipt_id=receipt,human_confirmed=True,**auth)
        worker=threading.Thread(target=edit);worker.start();assert started.wait(3)
        assert not done.wait(.15)
    worker.join(8);assert not worker.is_alive() and not errors and done.is_set()
    with Session() as db:
        with pytest.raises(ValueError,match='stale'):verified_day_totals(db,item_id)


def test_fresh_session_decimal_scale_and_frozen_payment_after_source_change(committed_day):
    from app.models import PayrollPayment
    from app.overtime_day_award import stage_monthly_totals,verify_monthly_snapshot
    from app.payroll_workflow import add_payroll_payment
    Session,auth,item_id,attendance_id=committed_day
    receipt=prepared(Session,auth,item_id,'c')
    with Session.begin() as db:
        confirm_day_award(db,receipt_id=receipt,human_confirmed=True,**auth)
        stage_monthly_totals(db,item_id,rounding_contract='existing-monthly-half-up')
    # Receipt used integer2, stored Numeric returns2.000000 in a fresh session.
    with Session.begin() as db:
        item=db.get(PayrollItem,item_id);verify_monthly_snapshot(db,item)
        db.get(PayrollRun,item.payroll_run_id).status='closed'
    with Session.begin() as db:
        db.get(AttendanceDailyRecord,attendance_id).review_notes='Synthetic later correction'
    with Session.begin() as db:
        item=db.get(PayrollItem,item_id)
        add_payroll_payment(db,item,amount=Decimal(1),paid_date=date(2026,3,5),method='cash',reference='Synthetic first installment',actor='synthetic')
    with Session.begin() as db:
        item=db.get(PayrollItem,item_id)
        add_payroll_payment(db,item,amount=Decimal(1),paid_date=date(2026,3,6),method='cash',reference='Synthetic later installment',actor='synthetic')
        assert item.overtime_amount==Decimal('50.00')


def test_mixed_paid_unpaid_reopened_run_requires_daily_for_unpaid(committed_day):
    from app.models import AttendanceMonth,PayrollPayment
    from app.payroll_workflow import approve_payroll_run,reopen_payroll_run
    Session,auth,item_id,attendance_id=committed_day
    with Session.begin() as db:
        paid=db.get(PayrollItem,item_id);run=db.get(PayrollRun,paid.payroll_run_id);run.status='closed';run.overtime_daily_required=False
        paid.overtime_hours=Decimal(2);paid.overtime_amount=Decimal(50);paid.net_pay=Decimal(6050)
        db.add(PayrollPayment(payroll_item_id=paid.id,amount=1,paid_at=datetime(2026,3,5),method='cash',created_by='synthetic'))
        emp=Employee(code='SYN-UNPAID',full_name='Synthetic unpaid',department_id=db.get(Employee,paid.employee_id).department_id,position='Synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(6000),daily_hours=Decimal(8));db.add(emp);db.flush()
        item=PayrollItem(payroll_run_id=run.id,employee_id=emp.id,employee_code_snapshot=emp.code,employee_name_snapshot=emp.full_name,department_name_snapshot='Synthetic',position_snapshot='Synthetic',salary_basis=6000,base_salary=6000,daily_hours=8,overtime_hours=2,overtime_amount=50,overtime_approved=True);db.add(item)
        db.add(AttendanceMonth(period_start=run.period_start,period_end=run.period_end,status='approved',created_by='synthetic'));db.flush()
        reopen_payroll_run(db,run,actor='synthetic',reason='Synthetic correction')
        assert run.overtime_daily_required
        with pytest.raises(ValueError,match='daily review'):approve_payroll_run(db,run,actor='synthetic')
        assert paid.overtime_hours==2 and paid.overtime_amount==50 and paid.net_pay==6050


def test_database_predecessor_day_scope_refuses(committed_day):
    from sqlalchemy.exc import DBAPIError
    Session,auth,item_id,attendance_id=committed_day
    receipt=prepared(Session,auth,item_id,'d')
    with Session.begin() as db:confirm_day_award(db,receipt_id=receipt,human_confirmed=True,**auth)
    with Session.begin() as db:
        with pytest.raises(DBAPIError),db.begin_nested():
            db.execute(text("INSERT INTO overtime_day_review_receipts(id,item_id,work_date,actor_id,session_id,session_version,predecessor_id,proposal_json,source_digest,integrity_digest,created_at,expires_at) SELECT :new,item_id,work_date+1,actor_id,session_id,session_version,id,proposal_json,source_digest,integrity_digest,created_at,expires_at FROM overtime_day_review_receipts WHERE id=:old"),dict(new='e'*64,old=receipt))


def test_closed_daily_required_cannot_hide_monthly_only_amount(committed_day):
    from app.payroll_workflow import add_payroll_payment
    Session,auth,item_id,attendance_id=committed_day
    with Session.begin() as db:
        item=db.get(PayrollItem,item_id);run=db.get(PayrollRun,item.payroll_run_id)
        run.status='closed';run.overtime_daily_required=True;item.overtime_hours=2;item.overtime_amount=50
        with pytest.raises(ValueError,match='daily review'):
            add_payroll_payment(db,item,amount=Decimal(1),paid_date=date(2026,3,5),method='cash',reference='Synthetic bypass attempt',actor='synthetic')


def test_daily_simulation_keeps_amount_and_refuses_basis_hypothesis(committed_day):
    from app.overtime_day_award import stage_monthly_totals
    from app.payroll_simulation import simulate_run_adjustment
    Session,auth,item_id,attendance_id=committed_day
    receipt=prepared(Session,auth,item_id,'f')
    with Session.begin() as db:
        confirm_day_award(db,receipt_id=receipt,human_confirmed=True,**auth)
        item=stage_monthly_totals(db,item_id,rounding_contract='existing-monthly-half-up');run_id=item.payroll_run_id
    with Session.begin() as db:
        item=db.get(PayrollItem,item_id);before=(item.overtime_amount,item.overtime_hours,item.overtime_daily_digest,item.net_pay)
        result=simulate_run_adjustment(db,run_id,field='other_bonus',mode='add',value='100')
        assert Decimal(result['total_net_delta'])==100
        for field in ('performance_bonus','overtime_hours'):
            with pytest.raises(ValueError,match='day_review_simulation'):simulate_run_adjustment(db,run_id,field=field,mode='add',value='1')
        assert not db.dirty
        assert before==(item.overtime_amount,item.overtime_hours,item.overtime_daily_digest,item.net_pay)


def test_ai_actual_approve_cannot_bypass_daily_and_snapshot_binds_awards(committed_day):
    from app.models import AttendanceMonth
    from app.ai_gateway import build_confirmation_preview
    from app.ai_agent import execute_plan
    Session,auth,item_id,attendance_id=committed_day
    with Session.begin() as db:
        item=db.get(PayrollItem,item_id);run=db.get(PayrollRun,item.payroll_run_id);run.overtime_daily_required=True;run_id=run.id
        item.overtime_hours=2;item.overtime_amount=50;item.overtime_approved=True
        db.add(AttendanceMonth(period_start=run.period_start,period_end=run.period_end,status='approved',created_by='synthetic'))
    plan=dict(action='approve_payroll_run',payroll_run_id=run_id,reason='Synthetic qualification')
    with Session.begin() as db:
        before=build_confirmation_preview(plan,session=db)['snapshot']
        with pytest.raises(ValueError,match='daily review'):execute_plan(plan,actor='synthetic',db=db)
    receipt=prepared(Session,auth,item_id,'1')
    with Session.begin() as db:confirm_day_award(db,receipt_id=receipt,human_confirmed=True,**auth)
    with Session.begin() as db:
        after=build_confirmation_preview(plan,session=db)['snapshot']
        assert before!=after and before['current']['source_digest']!=after['current']['source_digest']


def test_ai_business_lock_order_refuses_busy_without_table_cycle(committed_day):
    from fastapi import HTTPException
    from app.ai_gateway import _lock_command_business_tables
    from app.restore_lock import acquire_credential_write_lock
    Session,auth,item_id,attendance_id=committed_day
    started=threading.Event();finished=threading.Event();errors=[]
    def ai():
        try:
            with Session.begin() as db:
                started.set();_lock_command_business_tables(db)
        except Exception as exc:errors.append(exc)
        finally:finished.set()
    with Session.begin() as daily:
        acquire_credential_write_lock(daily)
        worker=threading.Thread(target=ai);worker.start();assert started.wait(3)
        # Published admission refuses the busy advisory gate before table reads.
        assert finished.wait(3)
        assert len(errors)==1 and isinstance(errors[0],HTTPException)
        assert errors[0].status_code==503
        assert errors[0].detail=='النظام مشغول مؤقتًا، أعد المحاولة بعد قليل.'
        daily.execute(text("SET LOCAL lock_timeout='1500ms'"))
        daily.execute(text('LOCK TABLE payroll_items IN SHARE ROW EXCLUSIVE MODE'))
    worker.join(3)
    assert not worker.is_alive()
    # A later request proceeds after the holder releases, without a gate bypass.
    with Session.begin() as db:_lock_command_business_tables(db)


def test_legacy_paid_monthly_snapshot_can_finish_after_reopen_reclose(committed_day):
    from app.models import AttendanceMonth,PayrollPayment
    from app.payroll_workflow import add_payroll_payment,reopen_payroll_run,approve_payroll_run,close_payroll_run
    Session,auth,item_id,attendance_id=committed_day
    with Session.begin() as db:
        item=db.get(PayrollItem,item_id);run=db.get(PayrollRun,item.payroll_run_id)
        run.status='closed';run.overtime_daily_required=False
        item.overtime_hours=2;item.overtime_amount=50;item.overtime_approved=True;item.net_pay=6050
        db.add(PayrollPayment(payroll_item_id=item.id,amount=1,paid_at=datetime(2026,3,5),method='cash',created_by='synthetic'))
        db.add(AttendanceMonth(period_start=run.period_start,period_end=run.period_end,status='approved',created_by='synthetic'));db.flush()
        reopen_payroll_run(db,run,actor='synthetic',reason='Synthetic legacy partial payment')
        assert run.overtime_daily_required
        approve_payroll_run(db,run,actor='synthetic');close_payroll_run(db,run,actor='synthetic')
        add_payroll_payment(db,item,amount=Decimal(1),paid_date=date(2026,3,6),method='cash',reference='Synthetic legacy remaining installment',actor='synthetic')
        assert (item.overtime_hours,item.overtime_amount,item.net_pay)==(2,50,6050)
        assert item.overtime_daily_digest is None


def test_legacy_payment_correction_chain_stays_historical_paid(committed_day):
    from app.models import AttendanceMonth,PayrollPayment
    from app.overtime_day_award import item_has_payment
    from app.payroll_workflow import add_payroll_payment,correct_payroll_payment,reopen_payroll_run,approve_payroll_run,close_payroll_run,payment_totals
    Session,auth,item_id,attendance_id=committed_day
    with Session.begin() as db:
        item=db.get(PayrollItem,item_id);run=db.get(PayrollRun,item.payroll_run_id)
        run.status='closed';run.overtime_daily_required=False
        item.overtime_hours=2;item.overtime_amount=50;item.overtime_approved=True;item.net_pay=6050
        original=add_payroll_payment(db,item,amount=Decimal(3),paid_date=date(2026,3,5),method='cash',reference='Synthetic historical payment',actor='synthetic')
        first=correct_payroll_payment(db,original,corrected_amount=Decimal(2),reason='Synthetic correction',actor='synthetic')
        last=correct_payroll_payment(db,first,corrected_amount=Decimal(1),reason='Synthetic second correction',actor='synthetic')
        assert original.superseded_by_payment_id==first.id and first.superseded_by_payment_id==last.id
        assert item_has_payment(db,item.id) and payment_totals(db,item)[0]==Decimal(1)
        db.add(AttendanceMonth(period_start=run.period_start,period_end=run.period_end,status='approved',created_by='synthetic'));db.flush()
        reopen_payroll_run(db,run,actor='synthetic',reason='Synthetic corrected historical payment')
        approve_payroll_run(db,run,actor='synthetic');close_payroll_run(db,run,actor='synthetic')
        add_payroll_payment(db,item,amount=Decimal(1),paid_date=date(2026,3,6),method='cash',reference='Synthetic next installment',actor='synthetic')
        assert payment_totals(db,item)[0]==Decimal(2)
        assert (item.overtime_hours,item.overtime_amount,item.net_pay)==(2,50,6050)
