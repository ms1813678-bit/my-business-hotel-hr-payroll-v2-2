"""Real migrated PostgreSQL day-award service, no HTTP/monthly-posting claim."""
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import select,func
from app.models import (OvertimeDayAward,OvertimeDayReviewReceipt,AdminAccount,PayrollPayment,LeaveMonthClose,PayrollItem,AuditLog,utcnow)
from app.overtime_day_award import prepare_day_award,confirm_day_award
from tests.test_overtime_day_source import attendance
from tests.test_pay_basis_source import setup
from tests.test_leave_historical_evidence import history_context  # noqa:F401
from tests.test_department_workbook_store import context  # noqa:F401


def make(history_context):
    db,auth,item,run=setup(history_context);a=attendance(db,item)
    return db,auth,item,run,a

def args(auth,item,**kw):
    return dict(operation_id='a'*64,item_id=item.id,work_date=date(2026,2,2),manual_hours=Decimal(3),rate_divisor=8,reason='Explicit excess grant reason',**auth)|kw

def test_review_confirm_replay_no_monthly_posting(history_context):
    db,auth,item,run,a=make(history_context)
    before=(item.overtime_hours,item.overtime_amount,item.overtime_approved,item.net_pay)
    row=prepare_day_award(db,**args(auth,item));assert prepare_day_award(db,**args(auth,item)).id==row.id
    award=confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    assert Decimal(award.amount_unrounded)==Decimal('112.5') and award.manual_hours==3
    assert confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth).receipt_id==row.id
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==1
    assert (item.overtime_hours,item.overtime_amount,item.overtime_approved,item.net_pay)==before
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='overtime_day_awarded'))==1

@pytest.mark.parametrize('change',['performance','attendance','closed','paid','epoch','role','confirm','reason'])
def test_changed_or_unauthorized_confirmation_has_no_award(history_context,change):
    db,auth,item,run,a=make(history_context)
    if change=='reason':
        with pytest.raises(ValueError,match='reason'):prepare_day_award(db,**args(auth,item,reason=''))
        return
    row=prepare_day_award(db,**args(auth,item))
    if change=='performance':item.performance_bonus+=1
    elif change=='attendance':a.review_notes='changed'
    elif change=='closed':run.status='closed'
    elif change=='paid':db.add(PayrollPayment(payroll_item_id=item.id,amount=Decimal(1),paid_at=utcnow(),method='cash',created_by='synthetic'))
    elif change=='epoch':auth=auth|{'expected_epoch':'stale'}
    elif change=='role':db.get(AdminAccount,auth['actor_id']).role='assistant_manager'
    db.flush()
    with pytest.raises(ValueError):confirm_day_award(db,receipt_id=row.id,human_confirmed=change!='confirm',**auth)
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==0


def test_finance_award_allowed_assistant_denied_by_actual_grant(history_context):
    db,auth,item,run,a=make(history_context);db.get(AdminAccount,auth['actor_id']).role='finance';db.flush()
    row=prepare_day_award(db,**args(auth,item));award=confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    assert award.confirmed_by==auth['actor_id']


def test_same_day_no_overwrite_and_receipt_tamper_blocked(history_context):
    db,auth,item,run,a=make(history_context);row=prepare_day_award(db,**args(auth,item));confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    with pytest.raises(ValueError,match='already awarded'):prepare_day_award(db,**args(auth,item,operation_id='b'*64))


@pytest.mark.parametrize('change',['expired','tampered','foreign_session'])
def test_receipt_failure_no_award(history_context,change):
    from datetime import UTC,datetime,timedelta
    from app.overtime_day_award import receipt_hash
    db,auth,item,run,a=make(history_context);row=prepare_day_award(db,**args(auth,item))
    # In-memory mutation with flush intentionally prohibited by DB trigger,
    # so exercise timeout via server clock rather than weaken immutability.
    if change=='expired':
        from unittest.mock import patch
        with patch('app.overtime_day_award.now',return_value=datetime.now(UTC)+timedelta(minutes=11)):
            with pytest.raises(ValueError,match='expired'):confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    elif change=='foreign_session':
        with pytest.raises(ValueError):confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**(auth|{'session_id':'foreign'}))
    else:
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError
        with pytest.raises(DBAPIError):
            with db.begin_nested():
                db.execute(text("UPDATE overtime_day_review_receipts SET source_digest=:digest WHERE id=:id"),dict(id=row.id,digest='0'*64))
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==0


@pytest.mark.parametrize('change',['missing','unapproved','incomplete','durationconflict','schedule','wrongday'])
def test_unsafe_attendance_cannot_prepare(history_context,change):
    db,auth,item,run,a=make(history_context)
    if change=='missing':db.delete(a)
    elif change=='unapproved':a.status='needs_review'
    elif change=='incomplete':a.approved_clock_out=None
    elif change=='durationconflict':a.worked_minutes=599
    elif change=='schedule':a.scheduled_start_at=None
    else:a.work_date=date(2026,2,3)
    db.flush()
    with pytest.raises(ValueError,match='attendance'):prepare_day_award(db,**args(auth,item))
    assert db.scalar(select(func.count()).select_from(OvertimeDayReviewReceipt))==0

@pytest.mark.parametrize('change',['inactive','version','password','pending','session','assistant'])
def test_current_account_rechecked_on_replay(history_context,change):
    from app.models import AdminSession
    db,auth,item,run,a=make(history_context);row=prepare_day_award(db,**args(auth,item))
    actor=db.get(AdminAccount,auth['actor_id'])
    if change=='inactive':actor.active=False
    elif change=='version':actor.session_version+=1
    elif change=='password':actor.must_change_password=True
    elif change=='pending':actor.pending_credential_reset=True
    elif change=='session':db.delete(db.get(AdminSession,auth['session_id']))
    else:actor.role='assistant_manager'
    db.flush()
    with pytest.raises(ValueError):prepare_day_award(db,**args(auth,item))
    with pytest.raises(ValueError):confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==0

@pytest.mark.parametrize('change',['divisor','fraction','boolean_hours','negative_basis','outside_period'])
def test_invalid_proposal_no_receipt(history_context,change):
    db,auth,item,run,a=make(history_context);params=args(auth,item)
    if change=='divisor':params['rate_divisor']=10
    elif change=='fraction':params['manual_hours']=Decimal('0.0000001')
    elif change=='boolean_hours':params['manual_hours']=True
    elif change=='negative_basis':
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                item.salary_basis=Decimal(-1);db.flush()
        return
    else:params['work_date']=date(2026,3,1)
    with pytest.raises(ValueError):prepare_day_award(db,**params)
    assert db.scalar(select(func.count()).select_from(OvertimeDayReviewReceipt))==0


def test_rollback_clears_receipt_award_audit(history_context):
    db,auth,item,run,a=make(history_context)
    before=db.scalar(select(func.count()).select_from(AuditLog))
    with db.begin_nested() as tx:
        row=prepare_day_award(db,**args(auth,item));confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth);tx.rollback()
    assert db.scalar(select(func.count()).select_from(OvertimeDayReviewReceipt))==0
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==0
    assert db.scalar(select(func.count()).select_from(AuditLog))==before


def test_award_update_delete_and_nonempty_downgrade_refuse(history_context):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    db,auth,item,run,a=make(history_context);row=prepare_day_award(db,**args(auth,item));confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    for sql in ('UPDATE overtime_day_awards SET manual_hours=4 WHERE receipt_id=:id','DELETE FROM overtime_day_awards WHERE receipt_id=:id'):
        with pytest.raises(DBAPIError):
            with db.begin_nested():db.execute(text(sql),dict(id=row.id))
    import importlib.util
    spec=importlib.util.spec_from_file_location('daily_migration','alembic/versions/0065_overtime_day_review.py');migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(db.connection())):
        with pytest.raises(RuntimeError,match='history'):migration.downgrade()


@pytest.mark.parametrize('divisor',[8,9,12])
def test_daily_divisors_and_excess_not_roster_cap(history_context,divisor):
    db,auth,item,run,a=make(history_context)
    row=prepare_day_award(db,**args(auth,item,manual_hours=Decimal(25),rate_divisor=divisor))
    award=confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec=250
        expected=(item.salary_basis+item.performance_bonus)/Decimal(30)/Decimal(divisor)*25
    assert Decimal(award.amount_unrounded)==expected


def test_distinct_day_does_not_stale_review(history_context):
    from datetime import timedelta
    db,auth,item,run,a=make(history_context)
    row=prepare_day_award(db,**args(auth,item))
    from app.models import AttendanceDailyRecord
    other=AttendanceDailyRecord(employee_id=item.employee_id,work_date=a.work_date+timedelta(days=1),status='approved',scheduled_start_at=a.scheduled_start_at+timedelta(days=1),scheduled_end_at=a.scheduled_end_at+timedelta(days=1),approved_clock_in=a.approved_clock_in+timedelta(days=1),approved_clock_out=a.approved_clock_out+timedelta(days=1),worked_minutes=a.worked_minutes)
    db.add(other);db.flush()
    row2=prepare_day_award(db,**args(auth,item,operation_id='b'*64,work_date=other.work_date,rate_divisor=12))
    confirm_day_award(db,receipt_id=row2.id,human_confirmed=True,**auth)
    confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==2


def test_leave_closed_month_after_review_refuses(history_context):
    from app.models import BackupRun
    db,auth,item,run,a=make(history_context);row=prepare_day_award(db,**args(auth,item))
    backup=BackupRun(backup_type='manual',status='success',size_bytes=0,initiated_by='synthetic');db.add(backup);db.flush()
    db.add(LeaveMonthClose(year=2026,month=2,backup_run_id=backup.id,closed_by='synthetic',closed_at=utcnow(),policy_snapshot_json='{}'));db.flush()
    with pytest.raises(ValueError,match='Closed leave month'):confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==0


def test_replay_after_state_advance_is_history_not_new_award(history_context):
    db,auth,item,run,a=make(history_context);row=prepare_day_award(db,**args(auth,item));award=confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    run.status='closed';a.review_notes='Changed after award';db.flush()
    assert prepare_day_award(db,**args(auth,item)).id==row.id
    assert confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth).receipt_id==award.receipt_id
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==1


def test_replacement_chain_and_financial_freshness_after_approval(history_context):
    from app.overtime_day_award import verified_day_totals,latest_day_award
    db,auth,item,run,a=make(history_context);row=prepare_day_award(db,**args(auth,item));old=confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    run.status='approved';db.flush()
    hours,total,digest=verified_day_totals(db,item.id);assert hours==3 and total==Decimal('112.5')
    run.status='reopened';item.performance_bonus=Decimal(1500);db.flush()
    with pytest.raises(ValueError,match='stale'):verified_day_totals(db,item.id)
    row2=prepare_day_award(db,**args(auth,item,operation_id='b'*64,predecessor_id=old.receipt_id,manual_hours=Decimal(2),reason='Corrected performance and hours'))
    new=confirm_day_award(db,receipt_id=row2.id,human_confirmed=True,**auth)
    assert latest_day_award(db,item.id,a.work_date).receipt_id==new.receipt_id
    hours,total,digest2=verified_day_totals(db,item.id);assert hours==2 and total==Decimal('62.5') and digest!=digest2
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==2
    with pytest.raises(ValueError,match='predecessor'):prepare_day_award(db,**args(auth,item,operation_id='c'*64,predecessor_id=old.receipt_id))



def test_explicit_monthly_contract_preserves_daily_divisors_and_precision(history_context):
    from app.overtime_day_award import stage_monthly_totals,verify_monthly_snapshot
    from datetime import timedelta
    from app.models import AttendanceDailyRecord
    db,auth,item,run,a=make(history_context)
    row=prepare_day_award(db,**args(auth,item,manual_hours=Decimal('0.123456'),rate_divisor=9));confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    other=AttendanceDailyRecord(employee_id=item.employee_id,work_date=a.work_date+timedelta(days=1),status='approved',scheduled_start_at=a.scheduled_start_at+timedelta(days=1),scheduled_end_at=a.scheduled_end_at+timedelta(days=1),approved_clock_in=a.approved_clock_in+timedelta(days=1),approved_clock_out=a.approved_clock_out+timedelta(days=1),worked_minutes=a.worked_minutes)
    db.add(other);db.flush()
    row2=prepare_day_award(db,**args(auth,item,operation_id='b'*64,work_date=other.work_date,manual_hours=Decimal('0.123456'),rate_divisor=12));confirm_day_award(db,receipt_id=row2.id,human_confirmed=True,**auth)
    with pytest.raises(ValueError,match='contract'):stage_monthly_totals(db,item.id,rounding_contract=None)
    stage_monthly_totals(db,item.id,rounding_contract='existing-monthly-half-up');db.flush();db.refresh(item)
    assert item.overtime_hours==Decimal('0.246912') and item.overtime_amount==Decimal('7.20')
    verify_monthly_snapshot(db,item)
    run.status='approved';db.flush();verify_monthly_snapshot(db,item)
    a.review_notes='Changed source';db.flush()
    with pytest.raises(ValueError,match='stale'):verify_monthly_snapshot(db,item)


def test_monthly_consumers_refuse_stale_daily_source_and_legacy_overwrite(history_context):
    from app.overtime_day_award import stage_monthly_totals
    from app.payroll_workflow import update_payroll_item,approve_overtime,close_payroll_run,add_payroll_payment
    db,auth,item,run,a=make(history_context);row=prepare_day_award(db,**args(auth,item));confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    stage_monthly_totals(db,item.id,rounding_contract='existing-monthly-half-up');db.flush()
    with pytest.raises(ValueError,match='daily|Daily'):approve_overtime(db,item,'synthetic')
    with pytest.raises(ValueError,match='day replacement'):update_payroll_item(db,item,{'overtime_hours':9},'synthetic')
    update_payroll_item(db,item,{'performance_bonus':1500},'synthetic');db.flush()
    assert not item.overtime_approved and item.overtime_amount==0
    run.status='approved';db.flush()
    with pytest.raises(ValueError,match='stale'):close_payroll_run(db,run,'synthetic')
    run.status='closed';db.flush()
    # A corrupted monthly amount still refuses; mutable attendance alone does not.
    with pytest.raises(ValueError,match='proof mismatch'):add_payroll_payment(db,item,amount=Decimal(1),paid_date=date(2026,3,5),method='cash',reference='synthetic',actor='synthetic')


def test_zero_hours_replacement_keeps_audited_history(history_context):
    from app.overtime_day_award import verified_day_totals
    db,auth,item,run,a=make(history_context);row=prepare_day_award(db,**args(auth,item));confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    replacement=prepare_day_award(db,**args(auth,item,operation_id='b'*64,predecessor_id=row.id,manual_hours=Decimal(0),reason='Correct mistaken day award to zero'))
    confirm_day_award(db,receipt_id=replacement.id,human_confirmed=True,**auth)
    hours,amount,digest=verified_day_totals(db,item.id);assert hours==0 and amount==0
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==2


def test_daily_review_replacement_monthly_approve_close_pay(history_context):
    from app.overtime_day_award import stage_monthly_totals
    from app.payroll_workflow import approve_payroll_run,close_payroll_run,add_payroll_payment
    from app.models import AttendanceMonth
    db,auth,item,run,a=make(history_context)
    row=prepare_day_award(db,**args(auth,item));confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**auth)
    replacement=prepare_day_award(db,**args(auth,item,operation_id='b'*64,predecessor_id=row.id,manual_hours=Decimal(2),rate_divisor=12,reason='Reviewed correction'))
    confirm_day_award(db,receipt_id=replacement.id,human_confirmed=True,**auth)
    stage_monthly_totals(db,item.id,rounding_contract='existing-monthly-half-up');db.flush()
    assert item.overtime_amount==50 and item.overtime_hours==2
    db.add(AttendanceMonth(period_start=run.period_start,period_end=run.period_end,status='approved',created_by='synthetic'));db.flush()
    approve_payroll_run(db,run,'synthetic');db.flush();assert run.status=='approved'
    close_payroll_run(db,run,'synthetic');db.flush();assert run.status=='closed'
    payment=add_payroll_payment(db,item,amount=Decimal(1),paid_date=date(2026,3,5),method='cash',reference='synthetic lifecycle',actor='synthetic');db.flush()
    assert payment.payroll_item_id==item.id and item.overtime_amount==50
