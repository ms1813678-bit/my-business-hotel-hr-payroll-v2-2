"""Unrouted global month readiness, not close approval or a backup substitute."""
from calendar import monthrange
from datetime import date
from hashlib import sha256
from sqlalchemy import select,text,func
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_payroll_days import read_payroll_days
from app.leave_payroll_read import get_month_summary
from app.leave_transition_draft import canonical
from app.models import Employee,EmployeeServicePeriod,LeaveRegisterEntry,LeaveMonthClose,PayrollRun


@bounded_workbook_operation
def prepare_month_close_readiness(db,*,year,month,employee_selections,**identity):
    """Current HR factual preparation, every relevant employee, no persisted effect.

    Missing service is never inferred from active/inactive status. An employee
    with no recorded service and a hire date by month-end remains unresolved.
    Explicit ended service with no target-month overlap is outside this month,
    unless target-month register evidence conflicts. Selection is optional but
    its absence blocks prepared source-summary coverage. No selection is chosen
    from latest policy, actor assertions, or a previous prepared receipt.
    """
    if db.new or db.dirty or db.deleted:raise ValueError('Clean read transaction required.')
    if (type(year) is not int or not 1<=year<=9999 or type(month) is not int or not 1<=month<=12
            or type(employee_selections) is not dict or len(employee_selections)>300
            or any(type(i) is not int or i<1 or type(s) is not dict for i,s in employee_selections.items())):
        raise ValueError('Bounded explicit month and employee selections required.')
    # The richer existing source-summary reader takes the exclusive credential
    # gate. Acquire it first, never upgrade two simultaneous shared read locks.
    # This unrouted preparation intentionally serializes with that reader.
    _guard(db,**identity)
    db.execute(text('LOCK TABLE employees,employee_service_periods,leave_register_entries,leave_month_notes,leave_month_close,payroll_runs IN SHARE MODE'))
    if (db.scalar(select(func.count()).select_from(Employee))>10000
            or db.scalar(select(func.count()).select_from(EmployeeServicePeriod))>30000):
        raise ValueError('Global evidence exceeds preparation bound.')
    first=date(year,month,1);last=date(year,month,monthrange(year,month)[1])
    employees=db.scalars(select(Employee).order_by(Employee.id)).all()
    periods=db.scalars(select(EmployeeServicePeriod)).all()
    byemployee={}
    for p in periods:byemployee.setdefault(p.employee_id,[]).append(p)
    ids={p.employee_id for p in periods if p.start_date<=last and (p.end_date is None or p.end_date>=first)}
    ids.update(db.scalars(select(LeaveRegisterEntry.employee_id).where(LeaveRegisterEntry.work_date>=first,LeaveRegisterEntry.work_date<=last)))
    ids.update(e.id for e in employees if not byemployee.get(e.id) and e.hire_date<=last)
    if len(ids)>300:raise ValueError('Global monthly employee scope exceeds preparation bound.')
    if set(employee_selections)-ids:raise ValueError('Selection outside relevant month scope.')
    rows=[]
    for e in employees:
        if e.id not in ids:continue
        days=read_payroll_days(db,employee_id=e.id,year=year,month=month,**identity)
        issues=[]
        if not days['ready']:issues.append('register_or_service_unresolved')
        summary_digest=None
        selected=employee_selections.get(e.id)
        if selected is None:issues.append('explicit_source_selection_missing')
        else:
            if (set(selected)&(set(identity)|{'employee_id','year','month'}) or selected.get('month_start')!=first):
                raise ValueError('Exact source period without identity override required.')
            try:
                summary=get_month_summary(db,employee_id=e.id,year=year,month=month,**selected,**identity)
                summary_digest=summary.source_digest
            except ValueError:
                issues.append('selected_source_summary_unresolved')
        rows.append(dict(employee_id=e.id,employee_code=e.code,day_source_digest=days['digest'],
            employed_days=days['evidence'].employed_days,unknown_days=days['evidence'].unknown_days,
            service_missing=days['evidence'].service_evidence_missing,service_overlap=days['overlapping_service'],
            conflicting_dates=days['evidence'].conflicting_dates,summary_digest=summary_digest,issues=tuple(issues)))
    closed=tuple(db.scalars(select(LeaveMonthClose.id).where(LeaveMonthClose.year==year,LeaveMonthClose.month==month)))
    payroll=tuple(db.scalars(select(PayrollRun.id).where(PayrollRun.period_start<=last,PayrollRun.period_end>=first,
        PayrollRun.status.in_(('approved','closed'))).order_by(PayrollRun.id)))
    blockers=[]
    if not rows:blockers.append('no_relevant_employee_evidence')
    if closed:blockers.append('month_already_closed')
    if payroll:blockers.append('approved_or_closed_payroll')
    if any(r['issues'] for r in rows):blockers.append('employee_source_coverage_incomplete')
    payload=dict(year=year,month=month,restore_epoch=identity['expected_epoch'],rows=rows,closed_ids=closed,payroll_ids=payroll,blockers=tuple(blockers))
    return dict(**payload,source_digest=sha256(canonical(payload).encode()).hexdigest(),
        source_coverage_prepared=not blockers,backup_verified=False,close_allowed=False,
        reconciliation_accepted=False,available_balance_established=False,payroll_money_allowed=False)
