"""Deterministic linkage candidates, never authorization to net two balances.

Only explicit record IDs establish a candidate. Coincident dates/amounts/names
are not linkage. Whole register days cannot allocate a fractional charged day.
"""
from dataclasses import dataclass
from datetime import date,timedelta
from decimal import Decimal


@dataclass(frozen=True)
class OverlapEvidence:
    leave_record_id: int
    ledger_ids: tuple[int,...]
    register_ids: tuple[int,...]
    state: str
    issues: tuple[str,...]
    evidence_sha256: str
    netting_allowed: bool = False
    employee_id: int | None = None
    category: str | None = None


def classify_record_overlap(*,record,ledger,register,basis,through,type_codes,tracked_types=None):
    """Supplied source rows only. Caller owns locks/currentness and digest."""
    if type(basis) is not date or type(through) is not date or basis>through:
        raise ValueError('Explicit overlap window required.')
    if not isinstance(record,dict) or type(record.get('id')) is not int:
        raise ValueError('Explicit source record required.')
    rid=record['id'];issues=[]
    linked_ledger=[r for r in ledger if r['leave_record_id']==rid]
    linked_register=[r for r in register if r['leave_record_id']==rid]
    # No date clipping allocation, even when counts happen to match.
    start,end=record['start_date'],record['end_date']
    if type(start) is not date or type(end) is not date or end<start:
        raise ValueError('Valid source record dates required.')
    if start<basis or end>through:
        issues.append('record_crosses_comparison_window_no_allocation')
    code=type_codes.get(record['leave_type_id'])
    if tracked_types is None or tracked_types.get(record['leave_type_id']) is not True:
        issues.append('record_type_not_balance_tracked')
    if code in {'A','H','W'} and sum(value==code for value in type_codes.values())!=1:
        issues.append('ambiguous_canonical_type_mapping')
    if code not in {'A','H','W'}:
        issues.append('record_category_unresolved')
    charge=record['charged_days']
    if not isinstance(charge,Decimal) or not charge.is_finite() or charge<=0:
        issues.append('invalid_nonpositive_or_nonfinite_record_charge')
    # Bound before expanding dates, source record can otherwise span millennia.
    count=(end-start).days+1
    if count>366:
        issues.append('record_date_span_requires_separate_review')
        expected=None
    else:
        expected={start+timedelta(days=n) for n in range(count)}
    valid_charge=isinstance(charge,Decimal) and charge.is_finite() and charge>0
    if valid_charge and charge!=charge.to_integral_value():
        issues.append('fractional_charge_cannot_map_to_whole_register_days')
    if not valid_charge or charge!=Decimal(count) or record['calendar_days']!=count:
        issues.append('charged_calendar_days_disagree')
    if any(r['employee_id']!=record['employee_id'] or r['leave_type_id']!=record['leave_type_id'] for r in linked_ledger):
        issues.append('ledger_record_identity_mismatch')
    if any(r['employee_id']!=record['employee_id'] or r['code']!=code for r in linked_register):
        issues.append('register_record_identity_or_code_mismatch')
    dates=[r['work_date'] for r in linked_register]
    if expected is None or set(dates)!=expected or len(dates)!=len(set(dates)):
        issues.append('linked_register_coverage_incomplete_or_conflicting')
    uses=[r for r in linked_ledger if r['transaction_type']=='leave_use']
    reversals=[r for r in linked_ledger if r['transaction_type']=='leave_reversal']
    if len(uses)!=1 or (uses and (not valid_charge or uses[0]['days_delta']!=charge.copy_negate())):
        issues.append('linked_use_amount_or_count_disagrees')
    if len(uses)+len(reversals)!=len(linked_ledger):
        issues.append('unexpected_linked_ledger_transaction')
    if record['status']=='confirmed':
        if reversals: issues.append('confirmed_record_has_reversal')
        state='explicit_link_candidate_not_reconciled' if not issues else 'unresolved_link_conflict'
    elif record['status']=='cancelled':
        if len(reversals)!=1 or (reversals and reversals[0]['days_delta']!=charge):
            issues.append('cancelled_record_reversal_disagrees')
        if linked_register:
            issues.append('cancelled_record_still_linked_in_register')
        # An exact use/reversal pair does not prove register removal or allocation.
        state='cancelled_record_requires_review'
    else:
        issues.append('record_not_confirmed');state='unresolved_link_conflict'
    import json
    from hashlib import sha256
    def encode(value):
        if isinstance(value,Decimal):return format(value,'f')
        if type(value) is date:return value.isoformat()
        raise TypeError('Unexpected overlap source value.')
    evidence=dict(record=record,ledger=sorted(linked_ledger,key=lambda r:r['id']),register=sorted(linked_register,key=lambda r:r['id']),
        basis=basis,through=through,type_codes=sorted(type_codes.items()),tracked_types=sorted((tracked_types or {}).items()))
    digest=sha256(json.dumps(evidence,sort_keys=True,separators=(',',':'),default=encode).encode()).hexdigest()
    return OverlapEvidence(rid,tuple(sorted(r['id'] for r in linked_ledger)),
        tuple(sorted(r['id'] for r in linked_register)),state,tuple(issues),digest,employee_id=record.get('employee_id'),category=code)


def read_overlap_candidates(db,*,employee_ids,basis,through):
    """Caller holds current-source transaction/authorization, never commits.

    Include all rows linked to each selected record, even conflicting foreign
    employees. Do not omit those rows and accidentally certify a partial view.
    """
    from sqlalchemy import select,func,or_,and_,text
    from app.models import LeaveRecord,LeaveRegisterEntry,LeaveBalanceTransaction,LeaveType
    if type(employee_ids) is not tuple or not 1<=len(employee_ids)<=300 or any(type(i) is not int or i<1 for i in employee_ids):
        raise ValueError('Bounded explicit employee scope required.')
    if type(basis) is not date or type(through) is not date or basis>through:
        raise ValueError('Explicit comparison window required.')
    db.execute(text('LOCK TABLE leave_records, leave_register_entries, leave_balance_transactions, leave_types IN SHARE MODE'))
    selected_ledger=select(LeaveBalanceTransaction.leave_record_id).where(LeaveBalanceTransaction.employee_id.in_(employee_ids),LeaveBalanceTransaction.leave_record_id.is_not(None))
    selected_register=select(LeaveRegisterEntry.leave_record_id).where(LeaveRegisterEntry.employee_id.in_(employee_ids),LeaveRegisterEntry.work_date>=basis,LeaveRegisterEntry.work_date<=through,LeaveRegisterEntry.leave_record_id.is_not(None))
    condition=or_(and_(LeaveRecord.employee_id.in_(employee_ids),LeaveRecord.start_date<=through,LeaveRecord.end_date>=basis),
        LeaveRecord.id.in_(selected_ledger),LeaveRecord.id.in_(selected_register))
    if db.scalar(select(func.count()).select_from(LeaveRecord).where(condition))>20000:
        raise ValueError('Bounded linked-record review scope exceeded.')
    records=db.scalars(select(LeaveRecord).where(condition).order_by(LeaveRecord.id)).all()
    ids=[r.id for r in records]
    for model in (LeaveRegisterEntry,LeaveBalanceTransaction):
        if db.scalar(select(func.count()).select_from(model).where(model.leave_record_id.in_(ids)))>100000:
            raise ValueError('Bounded linked-row review scope exceeded.')
    ledger=db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.leave_record_id.in_(ids)).order_by(LeaveBalanceTransaction.id)).all()
    register=db.scalars(select(LeaveRegisterEntry).where(LeaveRegisterEntry.leave_record_id.in_(ids)).order_by(LeaveRegisterEntry.id)).all()
    types=db.execute(select(LeaveType.id,LeaveType.code,LeaveType.tracks_balance)).all()
    codes={r.id:{'annual':'A','weekly_rest':'W','official_holiday':'H'}.get(r.code) for r in types}
    def values(row,keys):return {k:getattr(row,k) for k in keys}
    ledger_values=[values(r,('id','leave_record_id','employee_id','leave_type_id','transaction_type','days_delta')) for r in ledger]
    register_values=[values(r,('id','leave_record_id','employee_id','work_date','code')) for r in register]
    return tuple(classify_record_overlap(record=values(r,('id','employee_id','leave_type_id','start_date','end_date','calendar_days','charged_days','status')),
        ledger=ledger_values,register=register_values,basis=basis,through=through,type_codes=codes,tracked_types={t.id:t.tracks_balance for t in types}) for r in records)
