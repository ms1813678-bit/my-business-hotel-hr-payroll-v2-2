"""Saved item and same-day attendance comparison only, never an OT award."""
from dataclasses import dataclass,field
from datetime import date,timedelta
from decimal import Decimal,localcontext
from hashlib import sha256
import json
from sqlalchemy import select,text,inspect
from app.department_workbook_guard import bounded_workbook_operation
from app.models import PayrollItem,PayrollRun,AttendanceDailyRecord
from app.pay_basis_source import preview_stored_pay_basis
from app.overtime_policy_preview import preview_overtime_day
from app.time_semantics import local_elapsed_minutes_detail


@dataclass(frozen=True)
class StoredOvertimeDayComparison:
    payroll:object
    work_date:date
    attendance_id:int|None
    attendance_status:str|None
    attendance_snapshot:dict|None
    scheduled_minutes:int|None
    diagnostic_extra_hours:Decimal|None
    evidence_issues:tuple
    day_preview:object
    override_reason:str|None
    source_digest:str
    posting_allowed:bool=field(default=False,init=False)
    approval_allowed:bool=field(default=False,init=False)


def attendance_duration_diagnostic(row,work_date):
    """Duration surplus, not eligibility, break policy or approved overtime.

    Uses only stored approved endpoints, never first/last punch inference or a
    rate divisor as scheduled hours. Unknown or conflicting evidence stays None.
    """
    if row is None:return None,None,('same_day_record_missing',)
    if row.work_date!=work_date:raise ValueError('Exact same work date required.')
    issues=[]
    if row.status!='approved':issues.append('attendance_not_approved')
    start,end=row.scheduled_start_at,row.scheduled_end_at
    clock_in,clock_out=row.approved_clock_in,row.approved_clock_out
    scheduled=None
    if start is None or end is None:issues.append('schedule_endpoints_missing')
    elif start.date()!=work_date or end.date()>work_date+timedelta(days=1) or end<=start:issues.append('schedule_identity_or_order_invalid')
    else:
        scheduled,basis=local_elapsed_minutes_detail(start,end)
        if basis!='exact' or scheduled<=0:issues.append('schedule_duration_uncertain')
    if clock_in is None or clock_out is None:issues.append('approved_endpoints_missing')
    elif clock_in.date()!=work_date or clock_out.date()>work_date+timedelta(days=1) or clock_out<=clock_in:issues.append('approved_endpoint_order_invalid')
    else:
        actual,basis=local_elapsed_minutes_detail(clock_in,clock_out)
        if basis!='exact':issues.append('worked_duration_uncertain')
        if row.worked_minutes is None:issues.append('worked_minutes_missing')
        elif row.worked_minutes!=actual:issues.append('worked_minutes_endpoint_conflict')
    if issues:return scheduled,None,tuple(issues)
    with localcontext() as context:
        context.prec=250
        diagnostic=Decimal(max(0,row.worked_minutes-scheduled))/Decimal(60)
    return scheduled,diagnostic,()


@bounded_workbook_operation
def preview_stored_overtime_day(db,*,item_id,work_date,manual_hours,rate_divisor_hours,override_reason=None,**identity):
    if type(work_date) is not date:raise ValueError('Explicit same-day work date required.')
    if override_reason is not None and (not isinstance(override_reason,str) or len(override_reason)>2000):
        raise ValueError('Bounded optional reason text required.')
    payroll=preview_stored_pay_basis(db,item_id=item_id,cashout_days=Decimal(0),
        overtime_divisor_hours=rate_divisor_hours,manual_overtime_hours=manual_hours,**identity)
    # Consistent lock order: item/run first, then attendance. No write upgrades.
    db.execute(text('LOCK TABLE attendance_daily_records IN SHARE MODE'))
    from app.leave_policy_store import policy_read_guard
    policy_read_guard(db,**identity)
    item=db.get(PayrollItem,item_id,populate_existing=True)
    run=db.get(PayrollRun,item.payroll_run_id,populate_existing=True)
    if not run.period_start<=work_date<=run.period_end:raise ValueError('Work date outside saved payroll period.')
    row=db.scalar(select(AttendanceDailyRecord).where(AttendanceDailyRecord.employee_id==item.employee_id,
        AttendanceDailyRecord.work_date==work_date).execution_options(populate_existing=True))
    scheduled,extra,issues=attendance_duration_diagnostic(row,work_date)
    preview=preview_overtime_day(work_date=work_date,basic_salary=item.salary_basis,
        performance_bonus=item.performance_bonus,rate_divisor_hours=rate_divisor_hours,manual_hours=manual_hours,
        attendance_date=work_date if extra is not None else None,attendance_extra_hours=extra,override_reason=override_reason)
    # Keep the more precise bounded pay-basis arithmetic, not legacy 50-digit math.
    from dataclasses import replace
    with localcontext() as context:
        context.prec=250
        variance=None if extra is None else manual_hours-extra
    preview=replace(preview,variance_hours=variance,hourly_rate=payroll.policy_preview.overtime_hourly,
        amount_before_rounding=payroll.policy_preview.overtime_amount_unrounded)
    def serial(value):
        return value if value is None or type(value) in (str,int,bool) else str(value)
    snapshot=None if row is None else {c.key:serial(getattr(row,c.key)) for c in inspect(AttendanceDailyRecord).columns}
    payload=dict(payroll_digest=payroll.source_digest,attendance=snapshot,work_date=work_date.isoformat(),
        override_reason=override_reason,issues=issues)
    digest=sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return StoredOvertimeDayComparison(payroll,work_date,None if row is None else row.id,
        None if row is None else row.status,snapshot,scheduled,extra,issues,preview,override_reason,digest)
