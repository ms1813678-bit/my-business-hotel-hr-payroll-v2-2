"""Role-scoped monthly day evidence for payroll readers, no money/balance effect."""
from calendar import monthrange
from datetime import date
from dataclasses import asdict
from hashlib import sha256
from sqlalchemy import select,text,func
from app.department_workbook_guard import bounded_workbook_operation
from app.restore_lock import acquire_authenticated_read_lock,read_restore_epoch
from app.models import AdminAccount,AdminSession,EmployeeServicePeriod,LeaveRegisterEntry,LeaveMonthNote,Employee
from app.leave_read_model import register_evidence_summary
from app.leave_transition_draft import canonical


@bounded_workbook_operation
def read_payroll_days(db,*,employee_id,year,month,actor_id,session_id,session_version,expected_epoch):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean read transaction required.')
    if type(employee_id) is not int or employee_id<1 or type(year) is not int or not 1<=year<=9999 or type(month) is not int or not 1<=month<=12:raise ValueError('Exact period required.')
    acquire_authenticated_read_lock(db)
    actor=db.get(AdminAccount,actor_id,populate_existing=True);session=db.get(AdminSession,session_id,populate_existing=True)
    if (read_restore_epoch(db)!=expected_epoch or actor is None or session is None or session.admin_account_id!=actor.id
        or actor.role not in {'system_admin','finance'} or not actor.active or actor.must_change_password or actor.pending_credential_reset or actor.session_version!=session_version):raise ValueError('Current payroll read session required.')
    db.execute(text('LOCK TABLE employee_service_periods,leave_register_entries,leave_month_notes IN SHARE MODE'))
    employee=db.get(Employee,employee_id,populate_existing=True)
    if employee is None:raise ValueError('Employee missing.')
    if db.scalar(select(func.count()).select_from(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==employee_id))>1000:raise ValueError('Service evidence scope exceeds bound.')
    first=date(year,month,1);last=date(year,month,monthrange(year,month)[1])
    periods=db.scalars(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==employee_id)).all()
    overlaps=any(sum(p.start_date<=date(year,month,d) and (p.end_date is None or p.end_date>=date(year,month,d)) for p in periods)>1 for d in range(1,last.day+1))
    if db.scalar(select(func.count()).select_from(LeaveMonthNote).where(LeaveMonthNote.employee_id==employee_id,LeaveMonthNote.year==year,LeaveMonthNote.month==month))>100:raise ValueError('Monthly notes exceed bound.')
    evidence=register_evidence_summary(db,employee_id,first,last)
    entries=db.scalars(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==employee_id,
        LeaveRegisterEntry.work_date>=first,LeaveRegisterEntry.work_date<=last).order_by(LeaveRegisterEntry.work_date)).all()
    notes=db.scalars(select(LeaveMonthNote).where(LeaveMonthNote.employee_id==employee_id,
        LeaveMonthNote.year==year,LeaveMonthNote.month==month).order_by(LeaveMonthNote.id)).all()
    payload=asdict(evidence)
    # Bind the exact evidence, not just equal aggregate counts. This is a
    # display reference only, never a posted/verified receipt or approval.
    payload['service_rows']=sorted((p.id,p.start_date,p.end_date,p.start_reason,p.end_reason) for p in periods)
    payload['register_rows']=[(r.id,r.work_date,r.code,r.certificate_attached,r.notes,r.source,
        r.leave_record_id,r.revision,r.recorded_by,r.recorded_at.isoformat()) for r in entries]
    payload['month_note_rows']=[(n.id,n.notes,n.recorded_by,n.recorded_at.isoformat()) for n in notes]
    payload.update(employee_id=employee_id,month=first,overlapping_service=overlaps)
    ready=not (evidence.service_evidence_missing or evidence.unknown_days or evidence.conflicting_dates or overlaps)
    return dict(employee=dict(code=employee.code,name=employee.full_name),evidence=evidence,overlapping_service=overlaps,
        ready=ready,digest=sha256(canonical(payload).encode()).hexdigest(),month=first.strftime('%Y-%m'))
