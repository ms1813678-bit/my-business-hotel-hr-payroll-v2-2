"""Unwired day-only termination evidence. No settlement/deduction or posting."""
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json

from sqlalchemy import select,text,func

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_day_calculations import employed_dates
from app.leave_source_reconciliation import reconcile_selected_opening
from app.models import Employee,EmployeeServicePeriod,LeaveRegisterEntry,LeaveOpeningSelection


@dataclass(frozen=True)
class TerminationEvidence:
    employee_id: int
    end_work_date: date
    reconciliation: object
    negative_scenario_categories: tuple[str,...]
    annual_taken_since_hire: int | None
    unknown_service_days_since_hire: int
    six_month_boundary_explicit: date | None
    under_six_months: bool | None
    issues: tuple[str,...]
    evidence_sha256: str
    posting_allowed: bool = False
    automatic_deduction_allowed: bool = False
    settlement_allowed: bool = False


@bounded_workbook_operation
def read_termination_evidence(db,*,selection_id,department_id,end_work_date,
        current_insurance_years,age_evaluation_dates,release_evaluated_on,
        six_month_boundary=None,**identity):
    """Recorded service end must match the explicit date, no active-flag guess.

    Six-month calendar edge semantics are not selected here. The explicit boundary
    means the first day no longer under six months, equality is therefore false.
    This supplied boundary is not authenticated by arithmetic. Unknown
    employment/register history cannot become zero annual use. No rehire rule.
    """
    if type(end_work_date) is not date or (six_month_boundary is not None and type(six_month_boundary) is not date):
        raise ValueError('Explicit termination and optional reviewed boundary dates required.')
    from app.department_workbook_store import _guard
    _guard(db,**identity)
    db.execute(text('LOCK TABLE employees, employee_service_periods, leave_register_entries IN SHARE MODE'))
    selected=db.get(LeaveOpeningSelection,selection_id,populate_existing=True)
    if selected is None:raise ValueError('Explicit confirmed selection required.')
    employee=db.get(Employee,selected.employee_id,populate_existing=True)
    if db.scalar(select(func.count()).select_from(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==employee.id))>20000:
        raise ValueError('Bounded termination service history required.')
    if db.scalar(select(func.count()).select_from(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==employee.id))>100000:
        raise ValueError('Bounded termination register history required.')
    if employee is None or type(employee.hire_date) is not date:
        raise ValueError('Recorded employee original hire date required.')
    periods=db.scalars(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==employee.id)).all()
    if not periods or any(p.end_date is None or p.end_date>end_work_date or p.start_date>end_work_date for p in periods) or max(p.end_date for p in periods)!=end_work_date:
        raise ValueError('Termination requires matching final recorded service end and no later/open period.')
    if (end_work_date-employee.hire_date).days>36600 or employee.hire_date>end_work_date:
        raise ValueError('Bounded valid termination service history required.')
    result=reconcile_selected_opening(db,selection_ids=(selection_id,),department_id=department_id,
        month_start=end_work_date.replace(day=1),current_insurance_years=current_insurance_years,
        age_evaluation_dates=age_evaluation_dates,release_evaluated_on=release_evaluated_on,**identity)
    service=employed_dates(employee.hire_date,end_work_date,[(p.start_date,p.end_date) for p in periods])
    entries=db.execute(select(LeaveRegisterEntry.work_date,LeaveRegisterEntry.code).where(
        LeaveRegisterEntry.employee_id==employee.id,LeaveRegisterEntry.work_date>=employee.hire_date,
        LeaveRegisterEntry.work_date<=end_work_date)).all()
    by_date={row.work_date:row.code for row in entries}
    unknown=sum(day not in by_date or by_date[day] not in ('','A','H','W','S','U') for day in service)
    issues=['scenario_balances_not_spendable','no_automatic_deduction_or_settlement']
    if min(p.start_date for p in periods)!=employee.hire_date:
        issues.append('original_hire_and_first_service_start_disagree')
        taken_history_ambiguous=True
    else:
        taken_history_ambiguous=False
    outside=db.execute(select(LeaveRegisterEntry.work_date,LeaveRegisterEntry.code).where(
        LeaveRegisterEntry.employee_id==employee.id,LeaveRegisterEntry.work_date>end_work_date)).all()
    if any(r.code not in (None,'—') for r in outside):
        issues.append('register_evidence_after_final_service_end_requires_review')
    if unknown:issues.append('annual_use_history_incomplete_not_zero')
    if any(c.issues for c in result.overlap_candidates):
        issues.append('linked_legacy_leave_evidence_unresolved_no_register_inference')
    if six_month_boundary is None:
        short=None;issues.append('six_month_boundary_not_selected')
    else:
        if six_month_boundary<=employee.hire_date:
            raise ValueError('Reviewed six-month boundary must follow original hire.')
        short=end_work_date<six_month_boundary
    if len(periods)!=1:issues.append('multiple_service_periods_rehire_scope_unresolved')
    taken=None if unknown or taken_history_ambiguous else sum(by_date.get(day)=='A' for day in service)
    negative=tuple(r.code for r in result.rows if r.earned_scenario_balance<0 or r.released_scenario_balance<0)
    source=dict(reconciliation=result.reconciliation_digest,end_work_date=end_work_date.isoformat(),
        hire_date=employee.hire_date.isoformat(),post_end_register=[(r.work_date.isoformat(),r.code) for r in outside],register=[(r.work_date.isoformat(),r.code) for r in entries],
        six_month_boundary=six_month_boundary.isoformat() if six_month_boundary else None,annual_taken=taken,unknown=unknown,issues=issues)
    digest=sha256(json.dumps(source,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return TerminationEvidence(employee.id,end_work_date,result,negative,taken,unknown,six_month_boundary,short,tuple(issues),digest)
