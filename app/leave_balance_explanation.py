"""Explain earned/released/held and independent ledger evidence without netting."""
from dataclasses import dataclass,field
from decimal import Decimal
from datetime import date
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_source_reconciliation import reconcile_selected_opening


@dataclass(frozen=True)
class BalanceExplanationRow:
    employee_id:int
    code:str
    selected_opening:Decimal
    earned:Decimal
    released_earned:Decimal
    held_earned:Decimal
    registered_usage:Decimal
    earned_scenario:Decimal
    released_scenario:Decimal
    legacy_ledger_separate:Decimal|None
    ledger_row_ids:tuple
    overlap_candidates:tuple
    unresolved_reasons:tuple
    nonconflicting_raw_usage:Decimal|None=None
    current_balance:Decimal|None=None
    available_balance:Decimal|None=None
    netting_allowed:bool=field(default=False,init=False)


@dataclass(frozen=True)
class BalanceExplanation:
    rows:tuple
    source_digest:str
    earned_through:date
    release_evaluated_on:date
    opening_effective_start:date
    all_overlap_candidates:tuple=()
    posting_allowed:bool=field(default=False,init=False)
    establishes_spendable_credit:bool=field(default=False,init=False)


@bounded_workbook_operation
def explain_selected_balance(db,**selection):
    """Complete current register + exact confirmed opening/policy are required.

    Scenario totals include opening exactly once and registered usage once.
    Legacy ledger remains independent, even explicitly linked overlap remains
    a candidate, never an instruction to add, subtract or spend. Opening source
    confirmation establishes provenance, not release/ledger rebasing authority.
    """
    if db.new or db.dirty or db.deleted:raise ValueError('Clean read-only transaction required.')
    result=reconcile_selected_opening(db,**selection)
    rows=[]
    for r in result.rows:
        candidates=tuple(c for c in result.overlap_candidates if (c.employee_id==r.employee_id and c.category==r.code) or set(c.ledger_ids)&set(r.ledger_row_ids))
        reasons=list(r.issues)
        reasons.extend(('current_balance_not_established','available_balance_not_established'))
        rows.append(BalanceExplanationRow(r.employee_id,r.code,r.opening,r.earned_since_opening,
            r.released_earned_since_opening,r.held_earned_since_opening,r.used_since_opening,
            r.earned_scenario_balance,r.released_scenario_balance,r.ledger_total_separate,
            r.ledger_row_ids,candidates,tuple(dict.fromkeys(reasons)),nonconflicting_raw_usage=None if any(c.state!='explicit_link_candidate_not_reconciled' for c in candidates) or any('identity' in issue for c in result.overlap_candidates for issue in c.issues) else r.used_since_opening))
    return BalanceExplanation(tuple(rows),result.reconciliation_digest,result.earned_through,
        result.release_evaluated_on,result.opening_effective_start,result.overlap_candidates)
