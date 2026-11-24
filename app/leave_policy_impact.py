"""Read-only earned-day impact for an explicit policy draft, never recalculation commit."""
from dataclasses import dataclass,asdict
from datetime import date
from decimal import Decimal,localcontext
from hashlib import sha256
import json
from sqlalchemy import select,text,func
from app.department_preparation_workflow import _confirmed_department_projection,_manual_department_projection
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_policy_edit import read_policy_edit
from app.models import LeaveOpeningSelection,LeaveMonthClose,LeaveBalanceTransaction,EmployeeServicePeriod


@dataclass(frozen=True)
class EarnedPolicyImpactRow:
    employee_id:int
    code:str
    before_earned:Decimal
    after_earned:Decimal
    earned_delta:Decimal


@dataclass(frozen=True)
class EarnedPolicyImpact:
    rows:tuple
    source_digest:str
    closed_months:tuple
    draft_id:str
    reviewed_policy_sha256:str
    through:date
    limits:tuple=('earned_days_only','not_released_or_spendable_balance','no_year_end_cash_or_expiry_projection','no_sick_alert_projection','hypothetical_retroactive_including_closed_months','closed_records_not_modified','no_rebind_or_posting')
    posting_allowed:bool=False


@bounded_workbook_operation
def preview_earned_policy_impact(db,*,draft_id,reviewed_sha256,selection_ids,department_id,month_start,
        current_insurance_years,age_evaluation_dates,**identity):
    """Same locked live source, selected baseline must equal draft base version.

    Complete employed-day register coverage is required on both sides. Explicit
    insurance/age evaluation inputs use the existing source contract. This is a
    bounded accrued-day comparison, not a closed-month rewrite or balance commit.
    """
    if db.new or db.dirty or db.deleted:raise ValueError('Clean read-only transaction required.')
    _guard(db,**identity)
    if type(selection_ids) is not tuple or not 1<=len(selection_ids)<=300 or len(set(selection_ids))!=len(selection_ids):
        raise ValueError('Explicit bounded unique selections required.')
    db.execute(text('LOCK TABLE leave_month_close,leave_balance_transactions,employee_service_periods,leave_opening_selections,leave_manual_opening_groups,leave_historical_evidence_preparations,leave_policy_versions,leave_policy_edit_drafts IN SHARE MODE'))
    _guard(db,**identity)  # Recheck identity after any SHARE lock wait.
    draft,_before_policy,after_policy=read_policy_edit(db,draft_id=draft_id,**identity)
    if reviewed_sha256!=draft.review_sha256:raise ValueError('Exact reviewed draft required.')
    employee_ids=db.scalars(select(LeaveOpeningSelection.employee_id).where(LeaveOpeningSelection.id.in_(selection_ids))).all()
    if db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id.in_(employee_ids)))>100000:
        raise ValueError('Bounded ledger evidence exceeded.')
    if db.scalar(select(func.count()).select_from(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id.in_(employee_ids)))>20000:
        raise ValueError('Bounded service evidence exceeded.')
    old,_department=_confirmed_department_projection(db,selection_ids=selection_ids,department_id=department_id,
        month_start=month_start,current_insurance_years=current_insurance_years,age_evaluation_dates=age_evaluation_dates,**identity)
    selections=[db.get(LeaveOpeningSelection,i,populate_existing=True) for i in selection_ids]
    if any(s.policy_id!=draft.base_policy_id for s in selections):raise ValueError('Selected baseline must equal draft base policy.')
    new,_department=_manual_department_projection(db,group_ids=tuple(s.group_id for s in selections),department_id=department_id,
        month_start=month_start,policy=after_policy,current_insurance_years=current_insurance_years,age_evaluation_dates=age_evaluation_dates,**identity)
    if not old.policy_independent_source_sha256 or old.policy_independent_source_sha256!=new.policy_independent_source_sha256:
        raise ValueError('Identical policy-independent source evidence required.')
    if any(n for _,n in old.unresolved_target_days+new.unresolved_target_days):raise ValueError('Unknown employed days prevent earned impact preview.')
    if old.opening_effective_start!=new.opening_effective_start:raise ValueError('Identical opening basis required.')
    if tuple(e.employee_id for e in old.employees)!=tuple(e.employee_id for e in new.employees):raise ValueError('Identical selected employees required.')
    rows=[]
    with localcontext() as ctx:
        ctx.prec=100
        for left,right in zip(old.employees,new.employees):
            for code,attribute in [('A','annual'),('H','holiday'),('W','weekly')]:
                a=getattr(left,attribute).entitled_through_month_end
                b=getattr(right,attribute).entitled_through_month_end
                rows.append(EarnedPolicyImpactRow(left.employee_id,code,a,b,b-a))
    closed=tuple(db.execute(select(LeaveMonthClose.id,LeaveMonthClose.year,LeaveMonthClose.month,LeaveMonthClose.policy_snapshot_json)
        .where(LeaveMonthClose.year==month_start.year,LeaveMonthClose.month<=month_start.month).order_by(LeaveMonthClose.month)).all())
    closed_values=tuple(tuple(row) for row in closed)
    payload=dict(department_id=department_id,month_start=month_start.isoformat(),selection_ids=sorted(selection_ids),base_policy_id=draft.base_policy_id,base_sha256=draft.base_sha256,policy_independent_source=old.policy_independent_source_sha256,limits=EarnedPolicyImpact.__dataclass_fields__['limits'].default,before_source=old.source_digest,after_source=new.source_digest,draft_id=draft.id,
        draft_hash=draft.review_sha256,closed_months=closed_values,rows=[asdict(r) for r in rows],
        age_evaluation_dates=sorted((m.isoformat(),d.isoformat()) for m,d in age_evaluation_dates.items()),
        current_insurance_years=sorted((i,m.isoformat(),format(v,'f') if v is not None else None) for (i,m),v in current_insurance_years.items()))
    digest=sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
    from calendar import monthrange
    return EarnedPolicyImpact(tuple(rows),digest,closed_values,draft.id,draft.review_sha256,
        date(month_start.year,month_start.month,monthrange(month_start.year,month_start.month)[1]))
