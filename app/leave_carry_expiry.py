"""Review exact expired carry exclusion. No automatic expiry or ledger write.

The review names one recorded grant and its proven existing usage. It never
uses FIFO or assumes that an unrelated cash debit consumed that grant.
"""
from datetime import date, datetime
from fractions import Fraction
import json
import re
from sqlalchemy import select, text
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_month_earning import _locks, canonical, digest
from app.leave_exact_amount import canonical_amount, storage_amount, ledger_amount
from app.leave_carry_consumption import _grant, _usage, _verify_recorded, _refuse_unallocated_later_cash
from app.models import CAIRO_TZ, LeaveCarryAllocation, LeaveAnnualSettlement, LeaveCarryConsumption, LeaveRegisterEntry, LeaveType, LeaveMonthEarning, EmployeeServicePeriod, Employee, LeaveCarryExpiry, LeaveBalanceTransaction, AuditLog


def _derive(db, *, grant_kind,grant_id,employee_id,leave_type_id,evaluated_on,reason,identity):
    if (grant_kind not in {'carry','annual'} or not isinstance(grant_id,str)
            or re.fullmatch('[0-9a-f]{64}',grant_id) is None
            or type(employee_id) is not int or employee_id<1 or type(leave_type_id) is not int or leave_type_id<1
            or type(evaluated_on) is not date
            or evaluated_on>datetime.now(CAIRO_TZ).date() or not isinstance(reason,str)
            or not 1<=len(reason.strip())<=1000):
        raise ValueError('Explicit expired grant, current review date and bounded reason required.')
    if db.scalar(select(LeaveCarryExpiry.id).where(LeaveCarryExpiry.grant_kind==grant_kind,LeaveCarryExpiry.grant_id==grant_id)):
        raise ValueError('Grant already has a recorded exclusion.')
    model=LeaveAnnualSettlement if grant_kind=='annual' else LeaveCarryAllocation
    row=db.get(model,grant_id,populate_existing=True)
    if row is None or row.employee_id!=employee_id or row.leave_type_id!=leave_type_id:
        raise ValueError('Actual own employee/type grant required.')
    saved=json.loads(row.result_json)
    plan=saved['plan'] if grant_kind=='annual' else saved
    if grant_kind=='annual' and plan['annual']['late_year_preservation']:
        end=date(row.source_year+1,12,31)
    else:
        end=date.fromisoformat(plan['available_through'])
    # Actual expiry must have happened, not merely a future selected date.
    if not end<evaluated_on<=datetime.now(CAIRO_TZ).date():
        raise ValueError('Grant remains available; no early expiry or presumed erasure.')
    grant=_grant(db,grant_kind=grant_kind,grant_id=grant_id,employee_id=employee_id,
        leave_type_id=leave_type_id,work_date=end,identity=identity)
    _refuse_unallocated_later_cash(db,grant_kind=grant_kind,grant_id=grant_id,
        employee_id=employee_id,leave_type_id=leave_type_id)
    rows=db.scalars(select(LeaveCarryConsumption).where(LeaveCarryConsumption.employee_id==employee_id,
        LeaveCarryConsumption.leave_type_id==leave_type_id,LeaveCarryConsumption.grant_kind==grant_kind,
        LeaveCarryConsumption.grant_id==grant_id).order_by(LeaveCarryConsumption.id).limit(10001)).all()
    if len(rows)>10000:raise ValueError('Expired grant usage exceeds bounded scope.')
    used=Fraction();bindings=[]
    for consumption in rows:
        _request,result=_verify_recorded(db,consumption);old=result['plan']
        if (old['grant']!=grant or _usage(db,monthly_receipt_id=old['monthly_receipt_id'],
                usage_ledger_id=consumption.usage_ledger_id,employee_id=employee_id,leave_type_id=leave_type_id,
                work_date=consumption.work_date,identity=identity)!=old['usage']):
            raise ValueError('Expired grant usage evidence changed.')
        used+=Fraction(consumption.quantity_canonical)
        bindings.append([consumption.id,consumption.source_digest,consumption.result_digest])
    # A monthly usage debit may already have spent these days without an
    # attribution receipt. Never subtract an assumed unconsumed remainder.
    kind=db.get(LeaveType,leave_type_id,populate_existing=True)
    code={'annual':'A','weekly_rest':'W','official_holiday':'H'}.get(kind.code if kind else None)
    start=date.fromisoformat(grant['available_from'])
    from app.leave_year_coverage import required_earning_months
    from app.leave_month_earning import _verify_receipt, _earning_binding
    from app.leave_opening_source import read_confirmed_opening_source
    from app.department_workbook_source import read_workbook_source
    periods=db.scalars(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==employee_id)).all()
    expected=set()
    for year in range(start.year,end.year+1):
        required=required_earning_months(source_year=year,baseline=max(start,date(year,1,1)),
            service_periods=tuple((p.start_date,p.end_date) for p in periods),reviewed_on=date(year+1,1,1))
        expected.update(m for m in required if m<=end.replace(day=1))
    months=db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id==employee_id,
        LeaveMonthEarning.month_start>=start.replace(day=1),LeaveMonthEarning.month_start<=end.replace(day=1))).all()
    if {m.month_start for m in months}!=expected:
        raise ValueError('Complete employed-month usage coverage required through grant expiry.')
    employee=db.get(Employee,employee_id,populate_existing=True)
    monthly_bindings=[]
    for month in months:
        _verify_receipt(db,month)
        from app.leave_opening_rebind import current_descendant
        head=current_descendant(db,month.selection_id,identity['expected_epoch'])
        if head is None:raise ValueError('Current confirmed opening lineage required.')
        _reviews,_policy,policy_digest=read_confirmed_opening_source(db,selection_id=head,**identity)
        facts=read_workbook_source(db,department_id=employee.department_id,employee_ids=(employee_id,),
            window_start=date(month.month_start.year,1,1),month_start=month.month_start,policy_digest=policy_digest,
            expected_epoch=identity['expected_epoch'])
        if _earning_binding(json.loads(facts.canonical_json),month.month_start)!=json.loads(month.source_json)['earning_binding']:
            raise ValueError('Grant-window monthly usage source changed.')
        monthly_bindings.append([month.id,month.source_digest,month.result_digest])
    entries=db.scalars(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==employee_id,
        LeaveRegisterEntry.work_date>=start,LeaveRegisterEntry.work_date<=end,LeaveRegisterEntry.code==code).limit(10001)).all()
    if code is None or len(entries)>10000:raise ValueError('Bounded canonical usage evidence required.')
    all_allocations=db.scalars(select(LeaveCarryConsumption).where(LeaveCarryConsumption.employee_id==employee_id,
        LeaveCarryConsumption.leave_type_id==leave_type_id,LeaveCarryConsumption.work_date>=start,
        LeaveCarryConsumption.work_date<=end).limit(10001)).all()
    if len(all_allocations)>10000:raise ValueError('Attribution review scope exceeded.')
    by_day={};window_allocations=[]
    for allocation in all_allocations:
        _request,result=_verify_recorded(db,allocation);old=result['plan']
        actual_grant=_grant(db,grant_kind=allocation.grant_kind,grant_id=allocation.grant_id,
            employee_id=employee_id,leave_type_id=leave_type_id,work_date=allocation.work_date,identity=identity)
        if actual_grant!=old['grant'] or _usage(db,monthly_receipt_id=old['monthly_receipt_id'],
                usage_ledger_id=allocation.usage_ledger_id,employee_id=employee_id,leave_type_id=leave_type_id,
                work_date=allocation.work_date,identity=identity)!=old['usage']:
            raise ValueError('Relevant usage attribution changed.')
        window_allocations.append([allocation.id,allocation.source_digest,allocation.result_digest])
        by_day[allocation.work_date]=by_day.get(allocation.work_date,Fraction())+Fraction(allocation.quantity_canonical)
    if any(by_day.get(entry.work_date,Fraction())!=1 for entry in entries):
        raise ValueError('Unallocated register usage could already consume this grant; reviewed source allocation required before expiry exclusion.')
    # No unrelated late debit may secretly have spent this grant. Monthly
    # usage is accounted above; another historical or legacy debit is not FIFO.
    saved_source=saved['plan']['annual']['source']['source'] if grant_kind=='annual' else saved['source']['source']
    proven={entry[0] for entry in saved_source['rows']}
    if grant_kind=='annual' and row.cash_out_id:
        from app.models import LeaveCashOut
        cash=db.get(LeaveCashOut,row.cash_out_id,populate_existing=True)
        if cash is None:raise ValueError('Annual grant cash proof missing.')
        from app.leave_cash_out import _verify_cash_receipt
        _verify_cash_receipt(db,cash);proven.add(cash.ledger_id)
    for month in db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id==employee_id)).all():
        proven.update(item['id'] for item in _verify_receipt(db,month)['ledger'])
    from app.leave_zero_decision import verified_zero_ledger_ids
    proven.update(verified_zero_ledger_ids(db,employee_id))
    for expiry in db.scalars(select(LeaveCarryExpiry).where(LeaveCarryExpiry.employee_id==employee_id,
            LeaveCarryExpiry.leave_type_id==leave_type_id)).all():
        verify_expiry_receipt(db,expiry);proven.add(expiry.ledger_id)
    ledger=db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id==employee_id,
        LeaveBalanceTransaction.leave_type_id==leave_type_id).limit(10001)).all()
    if len(ledger)>10000 or any(entry.id not in proven for entry in ledger):
        raise ValueError('Unallocated later ledger may consume this grant; separate reviewed reconciliation required.')
    remainder=Fraction(grant['ceiling'])-used
    if remainder<=0:raise ValueError('No positive unconsumed expired grant remains to exclude.')
    return dict(grant_kind=grant_kind,grant_id=grant_id,employee_id=employee_id,leave_type_id=leave_type_id,
        grant_receipt_binding=[row.request_digest,row.result_digest],source_year=row.source_year,evaluated_on=evaluated_on.isoformat(),grant=grant,
        verified_consumption=bindings,verified_window_allocations=window_allocations,verified_window_months=monthly_bindings,consumed=canonical_amount(used),excluded=canonical_amount(remainder),
        proposed_ledger_delta=canonical_amount(-remainder),reason=reason.strip(),
        cash_amount='0/1',automatic_expiry=False)


@bounded_workbook_operation
def review_carry_expiry(db, **selection):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean expiry review transaction required.')
    identity={k:selection.pop(k) for k in ('actor_id','session_id','session_version','expected_epoch')}
    _locks(db,identity)
    db.execute(text('LOCK TABLE leave_carry_consumptions,leave_carry_expiries IN SHARE ROW EXCLUSIVE MODE'))
    plan=_derive(db,identity=identity,**selection)
    return dict(plan=plan,reviewed_digest=digest(plan),posting_allowed=False)


def verify_expiry_receipt(db, row):
    """Verify frozen posting evidence, not a new current-balance calculation."""
    if row is None:raise ValueError('Recorded grant exclusion missing.')
    request=json.loads(row.request_json);result=json.loads(row.result_json);plan=result['plan']
    ledger=db.get(LeaveBalanceTransaction,row.ledger_id,populate_existing=True)
    audits=db.scalars(select(AuditLog).where(AuditLog.action=='leave_carry_expired',
        AuditLog.entity_type=='leave_carry_expiry',AuditLog.entity_id==row.id)).all()
    if (digest(request)!=row.request_digest or digest(result)!=row.result_digest or digest(plan)!=row.source_digest
            or request['plan']!=plan or request['reviewed_digest']!=row.source_digest
            or (plan['employee_id'],plan['leave_type_id'],plan['grant_kind'],plan['grant_id'])!=
               (row.employee_id,row.leave_type_id,row.grant_kind,row.grant_id)
            or row.excluded_canonical!=plan['excluded'] or Fraction(plan['excluded'])<=0
            or Fraction(plan['excluded'])+Fraction(plan['consumed'])!=Fraction(plan['grant']['ceiling'])
            or plan['cash_amount']!='0/1' or plan['automatic_expiry'] is not False
            or result['cash_payment_committed'] is not False or result['ledger_id']!=row.ledger_id
            or ledger is None or ledger.employee_id!=row.employee_id or ledger.leave_type_id!=row.leave_type_id
            or ledger.source_operation_key!='leave_carry_expiry:'+row.id or ledger.source_digest!=row.source_digest
            or ledger.transaction_type!='adjustment' or ledger.earning_year!=plan['source_year']
            or ledger.released_on!=date.fromisoformat(plan['evaluated_on'])
            or canonical_amount(ledger_amount(ledger))!=plan['proposed_ledger_delta']
            or Fraction(ledger_amount(ledger))!=-Fraction(plan['excluded'])
            or len(audits)!=1 or audits[0].old_value!=row.source_digest or audits[0].new_value!=row.result_json
            or audits[0].actor!=request['actor_username'] or ledger.created_by!=request['actor_username']
            or audits[0].reason!=plan['reason'] or ledger.reason!=plan['reason']):
        raise ValueError('Recorded grant exclusion evidence changed.')
    model=LeaveAnnualSettlement if row.grant_kind=='annual' else LeaveCarryAllocation
    grant=model and db.get(model,row.grant_id,populate_existing=True)
    if (grant is None or [grant.request_digest,grant.result_digest]!=plan['grant_receipt_binding']
            or digest(json.loads(grant.request_json))!=grant.request_digest
            or digest(json.loads(grant.result_json))!=grant.result_digest):
        raise ValueError('Recorded exclusion grant changed.')
    from app.leave_recorded_source_versions import verify_versioned_recorded_source
    saved_grant=json.loads(grant.result_json)
    source_result=saved_grant
    if row.grant_kind=='annual':
        annual=saved_grant['plan']['annual']
        source_result=dict(source=annual['source'],source_digest=annual['source_digest'],
            carried_amount=annual['released_remaining'],held_annual=annual['held_remaining'],source_year=grant.source_year)
        from app.leave_annual_settlement_store import _verify_plan_rule
        _verify_plan_rule(db,annual,grant)
    verify_versioned_recorded_source(db,result=source_result,employee_id=row.employee_id,leave_type_id=row.leave_type_id)
    grant_action='leave_annual_settled' if row.grant_kind=='annual' else 'leave_carry_allocated'
    grant_entity='leave_annual_settlement' if row.grant_kind=='annual' else 'leave_carry_allocation'
    grant_audits=db.scalars(select(AuditLog).where(AuditLog.action==grant_action,
        AuditLog.entity_type==grant_entity,AuditLog.entity_id==grant.id)).all()
    if len(grant_audits)!=1 or grant_audits[0].new_value!=grant.result_json:
        raise ValueError('Recorded exclusion grant audit changed.')
    for receipt_id,source_digest,result_digest in plan['verified_window_allocations']:
        allocation=db.get(LeaveCarryConsumption,receipt_id,populate_existing=True)
        if allocation is None or allocation.source_digest!=source_digest or allocation.result_digest!=result_digest:
            raise ValueError('Recorded exclusion window allocation changed.')
        _verify_recorded(db,allocation)
    used=Fraction()
    for receipt_id,source_digest,result_digest in plan['verified_consumption']:
        receipt=db.get(LeaveCarryConsumption,receipt_id,populate_existing=True)
        if (receipt is None or receipt.source_digest!=source_digest or receipt.result_digest!=result_digest
                or receipt.grant_kind!=row.grant_kind or receipt.grant_id!=row.grant_id):
            raise ValueError('Recorded exclusion consumption changed.')
        _verify_recorded(db,receipt);used+=Fraction(receipt.quantity_canonical)
    if used!=Fraction(plan['consumed']):raise ValueError('Recorded exclusion usage amount changed.')
    for receipt_id,source_digest,result_digest in plan['verified_window_months']:
        from app.leave_month_earning import _verify_receipt
        month=db.get(LeaveMonthEarning,receipt_id,populate_existing=True)
        if month is None or month.source_digest!=source_digest or month.result_digest!=result_digest:
            raise ValueError('Recorded exclusion monthly coverage changed.')
        _verify_receipt(db,month)
    return result


@bounded_workbook_operation
def apply_carry_expiry(db, *, operation_id,reviewed_digest,human_confirmed,**selection):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean expiry transaction required.')
    if (not isinstance(operation_id,str) or re.fullmatch('[0-9a-f]{64}',operation_id) is None
            or not isinstance(reviewed_digest,str) or re.fullmatch('[0-9a-f]{64}',reviewed_digest) is None
            or human_confirmed is not True):raise ValueError('Explicit confirmed grant exclusion required.')
    identity={k:selection.pop(k) for k in ('actor_id','session_id','session_version','expected_epoch')}
    actor=_locks(db,identity)
    db.execute(text('LOCK TABLE leave_carry_consumptions,leave_carry_expiries IN SHARE ROW EXCLUSIVE MODE'))
    old=db.get(LeaveCarryExpiry,operation_id,populate_existing=True)
    normalized={k:v.isoformat() if type(v) is date else v for k,v in selection.items()}
    if old is not None:
        result=verify_expiry_receipt(db,old);request=json.loads(old.request_json)
        if (request['selection']!=normalized or request['identity']!=identity
                or request['actor_username']!=actor.username or old.source_digest!=reviewed_digest):
            raise ValueError('Grant exclusion retry identity changed.')
        return result
    plan=_derive(db,identity=identity,**selection)
    if digest(plan)!=reviewed_digest:raise ValueError('Grant exclusion source changed; review again.')
    request=dict(plan=plan,reviewed_digest=reviewed_digest,selection=normalized,identity=identity,actor_username=actor.username)
    with db.begin_nested():
        ledger=LeaveBalanceTransaction(employee_id=plan['employee_id'],leave_type_id=plan['leave_type_id'],
            transaction_type='adjustment',**storage_amount(-Fraction(plan['excluded'])),reason=plan['reason'],
            created_by=actor.username,source_operation_key='leave_carry_expiry:'+operation_id,source_digest=reviewed_digest,
            earning_year=plan['source_year'],released_on=selection['evaluated_on'])
        db.add(ledger);db.flush()
        result=dict(plan=plan,ledger_id=ledger.id,cash_payment_committed=False)
        row=LeaveCarryExpiry(id=operation_id,employee_id=plan['employee_id'],leave_type_id=plan['leave_type_id'],
            grant_kind=plan['grant_kind'],grant_id=plan['grant_id'],ledger_id=ledger.id,excluded_canonical=plan['excluded'],
            source_digest=reviewed_digest,request_json=canonical(request),request_digest=digest(request),
            result_json=canonical(result),result_digest=digest(result))
        db.add(row);db.add(AuditLog(actor=actor.username,action='leave_carry_expired',entity_type='leave_carry_expiry',
            entity_id=operation_id,old_value=reviewed_digest,new_value=row.result_json,reason=plan['reason']));db.flush()
    return result
