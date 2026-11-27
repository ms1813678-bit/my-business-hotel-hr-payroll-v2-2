"""Read-only selected-opening reconciliation, never a current spendable balance.

Earned scenario and legacy ledger are distinct views. No difference authorizes
an adjustment, no all-time ledger SUM is added to opening, no ledger is posted.
"""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, UTC
from decimal import Decimal, localcontext
from fractions import Fraction
from app.leave_exact_amount import exact_text
from hashlib import sha256
import json

from sqlalchemy import select,func,text

from app.department_preparation_workflow import _confirmed_department_projection
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_day_calculations import employed_dates
from app.leave_release_preview import annual_release_preview
from app.models import Employee, EmployeeServicePeriod, LeaveBalanceTransaction, LeaveRegisterEntry, LeaveType


MAX_LEDGER_ROWS=100000
MAX_SERVICE_ROWS=20000



# Exact earned reconstruction. The projection accumulates, in 50-digit Decimal, only terms
# whose exact denominators are fixed by the accrual contract: weekly (employed-U-S)/7,
# annual tier*(employed-deductible)/(12*days_in_month) with days_in_month in 28..31, and whole
# holiday days. Every possible exact sum therefore has a denominator dividing D below. Two distinct
# such fractions differ by at least 1/D**2 (about 5e-14 for annual), while the Decimal error over at
# most a few hundred monthly terms is below 1e-45, so the nearest fraction with denominator <= D is
# the one exact value. Nothing is allowed outside that structure: no dust tolerance.
_EXACT_DENOMINATOR={'A':4530960,'W':7,'H':1}  # A = 12*lcm(28,29,30,31), asserted in test
_RECONSTRUCTION_ERROR_LIMIT=Fraction(1,10**45)


def _exact_from_projection_decimal(value,code):
    if not isinstance(value,Decimal) or not value.is_finite() or value<0 or code not in _EXACT_DENOMINATOR:
        raise ValueError('Projection earned value is not an exact-reconstructible accrual.')
    exact=Fraction(value).limit_denominator(_EXACT_DENOMINATOR[code])
    if abs(Fraction(value)-exact)>_RECONSTRUCTION_ERROR_LIMIT or _EXACT_DENOMINATOR[code]%exact.denominator:
        raise ValueError('Projection earned value does not match its fixed accrual denominator structure.')
    return exact

@dataclass(frozen=True)
class CategoryReconciliation:
    employee_id: int
    code: str
    opening: Decimal
    earned_since_opening: Decimal
    released_earned_since_opening: Decimal
    held_earned_since_opening: Decimal
    used_since_opening: Decimal
    earned_scenario_balance: Decimal
    released_scenario_balance: Decimal
    ledger_total_separate: Decimal | None
    ledger_row_ids: tuple[int, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class SourceReconciliation:
    rows: tuple[CategoryReconciliation, ...]
    earned_through: date
    release_evaluated_on: date
    opening_effective_start: date
    source_digest: str
    reconciliation_digest: str
    overlap_candidates: tuple = ()
    establishes_spendable_credit: bool = False
    posting_allowed: bool = False


@bounded_workbook_operation
def reconcile_selected_opening(db, *, selection_ids, department_id, month_start,
        current_insurance_years, age_evaluation_dates, release_evaluated_on, **identity):
    """Locked current facts, confirmed selections, explicit dates, no persistence.

    Complete register evidence is required. Release date does not extend earned
    coverage beyond the selected month. Selected opening is a scenario source,
    never proof of released opening or proof of reconciliation with old ledger.
    """
    if type(selection_ids) is not tuple or not 1<=len(selection_ids)<=300 or any(not isinstance(i,str) or len(i)!=64 for i in selection_ids):
        raise ValueError('Explicit bounded confirmed selections required.')
    if type(month_start) is not date or month_start.day!=1:
        raise ValueError('Explicit first-of-month evidence period required.')
    end=date(month_start.year,month_start.month,monthrange(month_start.year,month_start.month)[1])
    if type(release_evaluated_on) is not date or release_evaluated_on<end:
        raise ValueError('Release evaluation cannot precede complete evidence month.')
    # Projection source-reader also materializes ledger facts. Count before it.
    from app.department_workbook_store import _guard
    from app.models import LeaveOpeningSelection
    _guard(db,**identity)
    db.execute(text('LOCK TABLE leave_balance_transactions, employee_service_periods, leave_register_entries, leave_records, leave_types IN SHARE MODE'))
    selected_ids=db.scalars(select(LeaveOpeningSelection.employee_id).where(LeaveOpeningSelection.id.in_(selection_ids))).all()
    if db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id.in_(selected_ids)))>MAX_LEDGER_ROWS:
        raise ValueError('Selected ledger evidence exceeds bounded reconciliation scope.')
    if db.scalar(select(func.count()).select_from(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id.in_(selected_ids)))>MAX_SERVICE_ROWS:
        raise ValueError('Selected service evidence exceeds bounded reconciliation scope.')
    projection,_department=_confirmed_department_projection(db,selection_ids=selection_ids,
        department_id=department_id,month_start=month_start,current_insurance_years=current_insurance_years,
        age_evaluation_dates=age_evaluation_dates,**identity)
    if any(count for _employee,count in projection.unresolved_target_days):
        raise ValueError('Unknown employed days prevent final earned reconciliation.')
    basis=projection.opening_effective_start
    rows=[]
    ids=[e.employee_id for e in projection.employees]
    employees={e.id:e for e in db.scalars(select(Employee).where(Employee.id.in_(ids)))}
    if db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id.in_(ids)))>MAX_LEDGER_ROWS:
        raise ValueError('Selected ledger evidence exceeds bounded reconciliation scope.')
    if db.scalar(select(func.count()).select_from(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id.in_(ids)))>MAX_SERVICE_ROWS:
        raise ValueError('Selected service evidence exceeds bounded reconciliation scope.')
    periods=db.scalars(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id.in_(ids))).all()
    uses=db.execute(select(LeaveRegisterEntry.employee_id,LeaveRegisterEntry.code).where(
        LeaveRegisterEntry.employee_id.in_(ids),LeaveRegisterEntry.work_date>=max(month_start,basis),
        LeaveRegisterEntry.work_date<=end)).all()
    kinds=db.execute(select(LeaveType.id,LeaveType.code,LeaveType.tracks_balance,LeaveType.active)).all()
    ledger=db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id.in_(ids))
        .order_by(LeaveBalanceTransaction.id)).all()
    for projected in projection.employees:
        employee=employees.get(projected.employee_id)
        if employee is None or type(employee.hire_date) is not date:raise ValueError('Current employee and recorded hire date required.')
        hired=employee.hire_date
        initial=annual_release_preview(hire_date=hired,as_of=release_evaluated_on,full_calendar_month_worked=None)
        # The immediately preceding calendar month is the first full month.
        first_month_end=date.fromordinal(initial.earliest_release_date.toordinal()-1)
        first_month_start=first_month_end.replace(day=1)
        service=[(p.start_date,p.end_date) for p in periods if p.employee_id==employee.id]
        full=employed_dates(first_month_start,first_month_end,service)
        released=annual_release_preview(hire_date=hired,as_of=release_evaluated_on,
            full_calendar_month_worked=len(full)==first_month_end.day)
        for code,snapshot in (('A',projected.annual),('H',projected.holiday),('W',projected.weekly)):
            # Canonical host codes seeded by migration0016, never display-name matching.
            host_code={'A':'annual','H':'official_holiday','W':'weekly_rest'}[code]
            kind_ids=[k.id for k in kinds if k.code==host_code]
            records=[r for r in ledger if r.employee_id==employee.id and r.leave_type_id in kind_ids]
            issues=['selected_opening_is_not_released_credit','ledger_overlap_not_reconciled']
            if any(k.id in kind_ids and not k.active for k in kinds):
                issues.append('category_inactive')
            if any(k.id in kind_ids and not k.tracks_balance for k in kinds):
                issues.append('category_not_balance_tracked')
            if len(kind_ids)!=1:
                total=None;issues.append('exact_ledger_category_unresolved')
            else:
                from app.leave_exact_amount import exact_sum, ledger_amount
                total = exact_sum(ledger_amount(r) for r in records)
            with localcontext() as context:
                context.prec=max(50,len(snapshot.carry.as_tuple().digits)+abs(snapshot.carry.as_tuple().exponent)+60)
                earned=snapshot.entitled_through_month_end
                released_earned=earned if code!='A' or released.released else Decimal(0)
                used=snapshot.used_before_month+Decimal(sum(i==employee.id and c==code for i,c in uses))
                earned_balance=snapshot.carry+earned-used
                released_balance=snapshot.carry+released_earned-used
                held=earned-released_earned
            ex=_exact_from_projection_decimal(earned,code)
            if Fraction(earned)!=ex:
                # Legacy 50-digit Decimal was inexact: use the uniquely determined exact rational.
                carry_f=Fraction(snapshot.carry);used_f=Fraction(used)
                rel_f=ex if code!='A' or released.released else Fraction(0)
                earned,released_earned,held=ex,rel_f,ex-rel_f
                earned_balance=carry_f+ex-used_f
                released_balance=carry_f+rel_f-used_f
            if code=='A' and not released.released:
                issues.append(released.reason)
            rows.append(CategoryReconciliation(employee.id,code,snapshot.carry,earned,released_earned,held,used,
                earned_balance,released_balance,total,tuple(r.id for r in records),tuple(issues)))
    from app.leave_overlap_evidence import read_overlap_candidates
    overlap=read_overlap_candidates(db,employee_ids=tuple(ids),basis=basis,through=end)
    from dataclasses import asdict
    payload={'overlap_candidates':[asdict(r) for r in overlap],'projection':projection.source_digest,'release_evaluated_on':release_evaluated_on.isoformat(),
        'rows':[asdict(r) for r in rows],
        'service_periods':sorted((p.id,p.employee_id,p.start_date.isoformat(),p.end_date.isoformat() if p.end_date else None) for p in periods),
        'ledger_categories':sorted((k.id,k.code,k.tracks_balance,k.active) for k in kinds),
        'ledger_rows':[(r.id,r.employee_id,r.leave_type_id,exact_text(ledger_amount(r)),r.transaction_type,r.leave_record_id,
            r.source_operation_key,r.source_digest,r.earning_year,r.released_on.isoformat() if r.released_on else None,r.reason,r.created_by,r.created_at.astimezone(UTC).isoformat() if r.created_at else None) for r in ledger]}
    def encode(value):
        if isinstance(value,(Decimal,Fraction)): return exact_text(value)
        raise TypeError('Unexpected reconciliation source value.')
    digest=sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),default=encode).encode()).hexdigest()
    return SourceReconciliation(tuple(rows),end,release_evaluated_on,basis,projection.source_digest,digest,overlap)
