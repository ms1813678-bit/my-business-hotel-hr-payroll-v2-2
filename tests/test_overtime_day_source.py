from datetime import date,datetime
from decimal import Decimal
from dataclasses import replace
import pytest
from sqlalchemy import select,func,inspect
from app.models import AttendanceDailyRecord,AuditLog
from app.overtime_day_source import preview_stored_overtime_day
from tests.test_pay_basis_source import setup
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_department_workbook_store import context  # noqa


def args(auth,item):
    return dict(item_id=item.id,work_date=date(2026,2,2),manual_hours=Decimal(3),rate_divisor_hours=8,**auth)


def attendance(db,item):
    row=AttendanceDailyRecord(employee_id=item.employee_id,work_date=date(2026,2,2),status='approved',
        scheduled_start_at=datetime(2026,2,2,9),scheduled_end_at=datetime(2026,2,2,17),
        approved_clock_in=datetime(2026,2,2,9),approved_clock_out=datetime(2026,2,2,19),worked_minutes=600)
    db.add(row);db.flush();return row


def test_source_exact_day_no_effect_and_excess_reason(history_context):
    db,auth,item,run=setup(history_context);row=attendance(db,item)
    def snapshot():return [[getattr(obj,c.key) for c in inspect(type(obj)).columns] for obj in (item,run,row)]
    before=snapshot();audit=db.scalar(select(func.count()).select_from(AuditLog))
    result=preview_stored_overtime_day(db,**args(auth,item))
    assert result.diagnostic_extra_hours==2 and result.day_preview.variance_hours==1
    assert result.day_preview.amount_before_rounding==Decimal('112.5')
    assert result.day_preview.review_issue=='excess_manual_hours_require_reason_and_authorized_review'
    reasoned=preview_stored_overtime_day(db,override_reason='Synthetic reason',**args(auth,item))
    assert reasoned.day_preview.review_issue=='excess_manual_hours_require_authorized_review'
    assert reasoned.source_digest!=result.source_digest
    assert not result.posting_allowed and not result.approval_allowed
    with pytest.raises(ValueError):replace(result,approval_allowed=True)
    assert snapshot()==before and db.scalar(select(func.count()).select_from(AuditLog))==audit


@pytest.mark.parametrize('change',['missing','unapproved','incomplete','durationconflict','schedule','outofworkday_endpoint'])
def test_uncertain_attendance_is_unknown_not_zero(history_context,change):
    db,auth,item,run=setup(history_context)
    if change!='missing':
        row=attendance(db,item)
        if change=='unapproved':row.status='needs_review'
        elif change=='incomplete':row.approved_clock_out=None
        elif change=='durationconflict':row.worked_minutes=599
        elif change=='schedule':row.scheduled_start_at=None
        elif change=='outofworkday_endpoint':row.approved_clock_out=datetime(2026,10,29,23,30)
        db.flush()
    result=preview_stored_overtime_day(db,**args(auth,item))
    assert result.diagnostic_extra_hours is None and result.day_preview.variance_hours is None
    assert result.evidence_issues and result.day_preview.review_issue=='same_day_attendance_evidence_missing'


def test_other_day_not_used_period_guard_and_changed_fact_digest(history_context):
    db,auth,item,run=setup(history_context);row=attendance(db,item)
    first=preview_stored_overtime_day(db,**args(auth,item))
    other=preview_stored_overtime_day(db,**(args(auth,item)|{'work_date':date(2026,2,3)}))
    assert other.attendance_id is None and other.diagnostic_extra_hours is None
    with pytest.raises(ValueError):preview_stored_overtime_day(db,**(args(auth,item)|{'work_date':date(2026,3,2)}))
    row.review_notes='Synthetic changed source';db.flush()
    changed=preview_stored_overtime_day(db,**args(auth,item))
    assert first.source_digest!=changed.source_digest


@pytest.mark.parametrize('change',['epoch','session','finance','wrongemployee'])
def test_current_authority_and_employee_binding(history_context,change):
    db,auth,item,run=setup(history_context);row=attendance(db,item)
    values=args(auth,item)
    if change=='epoch':values['expected_epoch']='synthetic-stale'
    elif change=='session':values['session_version']+=1
    elif change=='finance':
        from app.models import AdminAccount
        db.get(AdminAccount,auth['actor_id']).role='finance';db.flush()
    else:
        # Attendance belongs only to the saved item's employee, never another.
        from app.models import Employee
        employee=Employee(department_id=db.get(Employee,item.employee_id).department_id,code='SYN-OTHER-OT',full_name='Synthetic other',position='Synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8))
        db.add(employee);db.flush();row.employee_id=employee.id;db.flush()
        result=preview_stored_overtime_day(db,**values)
        assert result.attendance_id is None and result.diagnostic_extra_hours is None
        return
    with pytest.raises(ValueError):preview_stored_overtime_day(db,**values)


@pytest.mark.parametrize('transition',['ambiguous','nonexistent'])
def test_same_workday_dst_path_reached_not_date_order_refusal(history_context,transition):
    from app.time_semantics import local_elapsed_minutes_detail
    db,auth,item,run=setup(history_context);row=attendance(db,item)
    if transition=='ambiguous':
        workday=date(2026,10,29)
        run.period_start=date(2026,10,1);run.period_end=date(2026,10,31)
        row.scheduled_start_at=datetime(2026,10,29,16);row.scheduled_end_at=datetime(2026,10,30,0)
        row.approved_clock_in=datetime(2026,10,29,16);row.approved_clock_out=datetime(2026,10,29,23,30)
        expected='worked_duration_uncertain'
    else:
        workday=date(2026,4,24)
        run.period_start=date(2026,4,1);run.period_end=date(2026,4,30)
        row.scheduled_start_at=datetime(2026,4,24,0,30);row.scheduled_end_at=datetime(2026,4,24,8,30)
        row.approved_clock_in=datetime(2026,4,24,0,30);row.approved_clock_out=datetime(2026,4,24,10,30)
        expected='schedule_duration_uncertain'
    row.work_date=workday
    row.worked_minutes,basis=local_elapsed_minutes_detail(row.approved_clock_in,row.approved_clock_out)
    assert basis=='wall_fallback'
    db.flush()
    result=preview_stored_overtime_day(db,**(args(auth,item)|{'work_date':workday}))
    assert expected in result.evidence_issues
    assert 'approved_endpoint_order_invalid' not in result.evidence_issues
    assert 'schedule_identity_or_order_invalid' not in result.evidence_issues
    assert result.diagnostic_extra_hours is None and result.day_preview.variance_hours is None
