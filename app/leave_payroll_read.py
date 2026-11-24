"""Unrouted authenticated day-only payroll handoff, no money or availability.

The caller must select current confirmed opening/policy and explicit period.
Incomplete evidence refuses this final monthly handoff, never inferred as P/U.
Existing register evidence views remain available for resolving incomplete days.
"""
from calendar import monthrange
from dataclasses import dataclass, asdict, field
from datetime import date
from hashlib import sha256

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_balance_explanation import explain_selected_balance
from app.leave_read_model import register_evidence_summary
from app.leave_transition_draft import canonical


@dataclass(frozen=True)
class PayrollLeaveMonthSummary:
    employee_id: int
    year: int
    month: int
    opening_effective_start: date
    earned_through: date
    release_evaluated_on: date
    employed_days: int
    unknown_days: int
    present_days: int
    codes: tuple
    notes: tuple
    conflicting_dates: tuple
    balance_evidence: tuple
    source_digest: str
    available_balance_established: bool = field(default=False,init=False)
    payroll_money_allowed: bool = field(default=False,init=False)
    posting_allowed: bool = field(default=False,init=False)


@bounded_workbook_operation
def get_month_summary(db, *, employee_id, year, month, **selection):
    """Current HR-only internal source handoff, not an endpoint or a role grant.

    Counts describe ONLY the selected calendar month. Balance scenarios remain
    opening-basis-to-month-end, as explicitly named in the nested explanation.
    Legacy ledger stays separate. Earned/held evidence is not spendable credit.
    Callers cannot net it into payroll or interpret None as a zero balance.
    """
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean read-only transaction required.')
    if (type(employee_id) is not int or employee_id < 1 or type(year) is not int
            or not 1 <= year <= 9999 or type(month) is not int or not 1 <= month <= 12):
        raise ValueError('Exact employee and calendar year/month required.')
    first=date(year,month,1)
    if selection.get('month_start') != first:
        raise ValueError('Selected evidence period must equal requested payroll month.')
    from app.department_workbook_store import _guard
    from sqlalchemy import text
    _guard(db,**{k:selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')})
    db.execute(text('LOCK TABLE leave_month_notes IN SHARE MODE'))
    explanation=explain_selected_balance(db, **selection)
    if not explanation.rows or {r.employee_id for r in explanation.rows} != {employee_id} or {r.code for r in explanation.rows}!={'A','H','W'}:
        raise ValueError('Exactly the requested employee must be selected.')
    last=date(year,month,monthrange(year,month)[1])
    evidence=register_evidence_summary(db,employee_id,first,last)
    if evidence.service_evidence_missing or evidence.unknown_days or evidence.conflicting_dates:
        raise ValueError('Unresolved register/service evidence blocks final monthly handoff.')
    # Original reader uses no-autoflush; authorization and service/register locks
    # were acquired by the complete selected-source explanation above.
    payload=dict(employee_id=employee_id,year=year,month=month,explanation_source=explanation.source_digest,
                 earned_through=explanation.earned_through.isoformat(),
                 opening_effective_start=explanation.opening_effective_start.isoformat(),
                 release_evaluated_on=explanation.release_evaluated_on.isoformat(),
                 register=asdict(evidence),balances=[asdict(r) for r in explanation.rows])
    digest=sha256(canonical(payload).encode()).hexdigest()
    return PayrollLeaveMonthSummary(employee_id,year,month,explanation.opening_effective_start,explanation.earned_through,
        explanation.release_evaluated_on,evidence.employed_days,evidence.unknown_days,
        evidence.present_days,tuple(sorted(evidence.codes.items())),evidence.notes,evidence.conflicting_dates,
        explanation.rows,digest)
