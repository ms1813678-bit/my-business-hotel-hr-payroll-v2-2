"""Read-only global close-screen evidence. No selection, backup or close mutation."""
from calendar import monthrange
from datetime import date
from sqlalchemy import select,text,func
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_policy_store import policy_read_guard
from app.leave_payroll_days import read_payroll_days
from app.models import Employee,EmployeeServicePeriod,LeaveRegisterEntry,LeaveMonthClose,PayrollRun

@bounded_workbook_operation
def read_close_day_preview(db,*,month,**identity):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean read required.')
    policy_read_guard(db,**identity)
    db.execute(text('LOCK TABLE employees,employee_service_periods,leave_register_entries,leave_month_notes,leave_month_close,payroll_runs IN SHARE MODE'))
    if db.scalar(select(func.count()).select_from(Employee))>10000 or db.scalar(select(func.count()).select_from(EmployeeServicePeriod))>30000:
        raise ValueError('Global scope exceeds bound.')
    last=date(month.year,month.month,monthrange(month.year,month.month)[1])
    periods=db.scalars(select(EmployeeServicePeriod)).all();known={p.employee_id for p in periods}
    ids={p.employee_id for p in periods if p.start_date<=last and (p.end_date is None or p.end_date>=month)}
    ids.update(db.scalars(select(LeaveRegisterEntry.employee_id).where(LeaveRegisterEntry.work_date>=month,LeaveRegisterEntry.work_date<=last)))
    employees=db.scalars(select(Employee).order_by(Employee.id)).all()
    ids.update(e.id for e in employees if e.id not in known and e.hire_date<=last)
    if len(ids)>300:raise ValueError('Monthly scope exceeds bound.')
    rows=[]
    for e in employees:
        if e.id in ids:
            result=read_payroll_days(db,employee_id=e.id,year=month.year,month=month.month,**identity)
            rows.append(dict(employee_id=e.id,code=e.code,name=e.full_name,**{k:result[k] for k in ('evidence','ready','overlapping_service','digest')}))
    return dict(rows=rows,closed=bool(db.scalar(select(LeaveMonthClose.id).where(LeaveMonthClose.year==month.year,LeaveMonthClose.month==month.month))),
        payroll_blocked=bool(db.scalar(select(PayrollRun.id).where(PayrollRun.period_start<=last,PayrollRun.period_end>=month,PayrollRun.status.in_(('approved','closed'))))),
        backup_verified=False,source_reconciliation_accepted=False,close_allowed=False)
