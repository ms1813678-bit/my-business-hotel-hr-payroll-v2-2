"""Explicit exact attribution of EXISTING register usage to a chosen carry grant.

No second debit, FIFO, attendance edit, cash, automatic expiry or new role.
Caller owns the transaction. A monthly aggregate usage row remains the only
ledger debit, while work-date allocations prove which grant it consumed.
"""
from datetime import date,datetime
from fractions import Fraction
import json
import re
from sqlalchemy import select,text
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_month_earning import _locks,_verify_receipt,_earning_binding,canonical,digest,CATEGORIES
from app.leave_exact_amount import as_fraction,canonical_amount,ledger_amount
from app.models import (AuditLog,Employee,LeaveType,LeaveMonthEarning,LeaveRegisterEntry,
    LeaveBalanceTransaction,LeaveCarryConsumption,LeaveAnnualSettlement,LeaveCashOut)


def _grant(db, *, grant_kind, grant_id, employee_id, leave_type_id, work_date, identity):
    if grant_kind=='carry':
        from app.leave_carry_availability import read_carry_availability
        result=read_carry_availability(db,allocation_id=grant_id,employee_id=employee_id,
            leave_type_id=leave_type_id,work_date=work_date,**identity)
        if not result['eligible']:raise ValueError('Carry grant is not positive and available on this work date.')
        return dict(kind=grant_kind,id=grant_id,employee_id=employee_id,leave_type_id=leave_type_id,
            ceiling=result['source_amount'],source_digest=result['source_digest'],
            available_from=result['available_from'],available_through=result['available_through'])
    if grant_kind!='annual':raise ValueError('Explicit supported carry grant kind required.')
    row=db.get(LeaveAnnualSettlement,grant_id,populate_existing=True)
    if row is None or row.employee_id!=employee_id or row.leave_type_id!=leave_type_id:
        raise ValueError('Annual grant belongs to another employee or type.')
    from app.leave_year_transition import _closing_receipt
    _closing_receipt(db,employee_id=employee_id,leave_type_id=leave_type_id,source_year=row.source_year,
        reviewed_on=work_date,identity=identity)
    result=json.loads(row.result_json);plan=result['plan'];annual=plan['annual']
    # Verify the pre-cash source on its named rows, not today's enlarged ledger.
    from app.leave_carry_availability import _verify_recorded_source
    _verify_recorded_source(db,result=dict(source=annual['source'],source_digest=annual['source_digest'],
        carried_amount=annual['released_remaining'],held_annual=annual['held_remaining'],source_year=row.source_year),
        employee_id=employee_id,leave_type_id=leave_type_id)
    ceiling=Fraction(annual['carried_days'])
    if ceiling<=0:raise ValueError('Annual debt or zero is not a positive grant.')
    start=date.fromisoformat(plan['available_from']);end=date.fromisoformat(plan['available_through'])
    if annual['late_year_preservation']:
        start=date(row.source_year+1,1,1);end=date(row.source_year+1,12,31)
    if not start<=work_date<=end:raise ValueError('Annual grant unavailable on selected work date.')
    # Held amounts must have their real retroactive release credit, never merely
    # an opening balance or a calendar advancing past the first complete month.
    for receipt_id,amount in annual['source']['source']['held_annual']:
        receipt=db.get(LeaveMonthEarning,receipt_id,populate_existing=True)
        key=f'month_earning:{employee_id}:{receipt.month_start.isoformat()}:A:earned'
        release=db.scalar(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.source_operation_key==key))
        if release is None or canonical_amount(ledger_amount(release))!=amount:
            raise ValueError('Annual held grant requires its verified release credit.')
        verified=False
        for month in db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id==employee_id)).all():
            if any(item['id']==release.id for item in _verify_receipt(db,month)['ledger']):verified=True
        if not verified:raise ValueError('Annual grant release lacks monthly receipt proof.')
    return dict(kind=grant_kind,id=grant_id,employee_id=employee_id,leave_type_id=leave_type_id,
        ceiling=canonical_amount(ceiling),source_digest=row.source_digest,
        available_from=start.isoformat(),available_through=end.isoformat())


def _usage(db, *, monthly_receipt_id, usage_ledger_id, employee_id, leave_type_id, work_date, identity):
    month=db.get(LeaveMonthEarning,monthly_receipt_id,populate_existing=True)
    kind=db.get(LeaveType,leave_type_id,populate_existing=True)
    code=next((c for c,v in CATEGORIES.items() if kind and v==kind.code),None)
    if (month is None or month.employee_id!=employee_id or month.month_start!=work_date.replace(day=1)
            or code is None or not kind.active or not kind.tracks_balance):
        raise ValueError('Actual own-month canonical register usage required.')
    result=_verify_receipt(db,month)
    matches=[item for item in result['ledger'] if item['id']==usage_ledger_id and item['category']==code and item['kind']=='usage']
    ledger=db.get(LeaveBalanceTransaction,usage_ledger_id,populate_existing=True)
    entry=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==employee_id,LeaveRegisterEntry.work_date==work_date))
    if (len(matches)!=1 or ledger is None or ledger.employee_id!=employee_id or ledger.leave_type_id!=leave_type_id
            or ledger_amount(ledger)>=0 or entry is None or entry.code!=code):
        raise ValueError('Existing monthly debit and exact register-day code must agree.')
    from app.department_workbook_source import read_workbook_source
    from app.leave_opening_source import read_confirmed_opening_source
    _reviews,_policy,policy_digest=read_confirmed_opening_source(db,selection_id=month.selection_id,**identity)
    employee=db.get(Employee,employee_id,populate_existing=True)
    facts=read_workbook_source(db,department_id=employee.department_id,employee_ids=(employee_id,),
        window_start=date(work_date.year,1,1),month_start=month.month_start,policy_digest=policy_digest,
        expected_epoch=identity['expected_epoch'])
    if _earning_binding(json.loads(facts.canonical_json),month.month_start)!=json.loads(month.source_json)['earning_binding']:
        raise ValueError('Monthly usage register source changed.')
    return dict(monthly_receipt_id=month.id,source_digest=month.source_digest,result_digest=month.result_digest,
        ledger_id=ledger.id,amount=canonical_amount(-as_fraction(ledger_amount(ledger))),
        employee_id=employee_id,leave_type_id=leave_type_id,work_date=work_date.isoformat(),code=code)


def _verify_recorded(db,row):
    request=json.loads(row.request_json);result=json.loads(row.result_json)
    audits=db.scalars(select(AuditLog).where(AuditLog.action=='leave_carry_consumed',
        AuditLog.entity_type=='leave_carry_consumption',AuditLog.entity_id==row.id)).all()
    if (digest(request)!=row.request_digest or digest(result)!=row.result_digest or digest(result['plan'])!=row.source_digest
            or request['reviewed_digest']!=row.source_digest or request['plan']!=result['plan']
            or result['ledger_delta']!='0/1' or result['cash_payment_committed'] is not False
            or result['plan']['employee_id']!=row.employee_id or result['plan']['leave_type_id']!=row.leave_type_id
            or result['plan']['grant_kind']!=row.grant_kind or result['plan']['grant_id']!=row.grant_id
            or result['plan']['usage_ledger_id']!=row.usage_ledger_id
            or result['plan']['work_date']!=row.work_date.isoformat()
            or result['plan']['quantity']!=row.quantity_canonical
            or len(audits)!=1 or audits[0].actor!=request['actor_username']
            or audits[0].old_value!=row.source_digest or audits[0].new_value!=row.result_json
            or audits[0].reason!=result['plan']['reason']):raise ValueError('Carry consumption recorded evidence changed.')
    return request,result


def _derive(db, *, grant_kind,grant_id,employee_id,leave_type_id,monthly_receipt_id,usage_ledger_id,work_date,quantity,reason,identity):
    if (grant_kind not in {'carry','annual'} or not isinstance(grant_id,str) or re.fullmatch('[0-9a-f]{64}',grant_id) is None
            or type(employee_id) is not int or employee_id<1 or type(leave_type_id) is not int or leave_type_id<1
            or type(work_date) is not date or not isinstance(reason,str) or not 1<=len(reason.strip())<=1000):
        raise ValueError('Explicit grant, employee, type, work date and bounded reason required.')
    amount=as_fraction(quantity)
    if not 0<amount<=1:raise ValueError('Explicit positive exact attribution no greater than one register day required.')
    grant=_grant(db,grant_kind=grant_kind,grant_id=grant_id,employee_id=employee_id,leave_type_id=leave_type_id,work_date=work_date,identity=identity)
    _refuse_unallocated_later_cash(db,grant_kind=grant_kind,grant_id=grant_id,employee_id=employee_id,leave_type_id=leave_type_id)
    usage=_usage(db,monthly_receipt_id=monthly_receipt_id,usage_ledger_id=usage_ledger_id,employee_id=employee_id,
        leave_type_id=leave_type_id,work_date=work_date,identity=identity)
    grant_used=Fraction();day_used=Fraction();row_used=Fraction()
    bindings=[]
    for old in db.scalars(select(LeaveCarryConsumption).where(LeaveCarryConsumption.employee_id==employee_id,
            LeaveCarryConsumption.leave_type_id==leave_type_id).limit(10001)).all():
        if len(bindings)>=10000:raise ValueError('Carry attribution history exceeds bounded review scope.')
        _request,saved=_verify_recorded(db,old);oldplan=saved['plan']
        oldusage=_usage(db,monthly_receipt_id=oldplan['monthly_receipt_id'],usage_ledger_id=old.usage_ledger_id,
            employee_id=employee_id,leave_type_id=leave_type_id,work_date=old.work_date,identity=identity)
        if oldusage!=oldplan['usage']:raise ValueError('Earlier carry usage evidence changed.')
        if _grant(db,grant_kind=old.grant_kind,grant_id=old.grant_id,employee_id=employee_id,leave_type_id=leave_type_id,work_date=old.work_date,identity=identity)!=oldplan['grant']:
            raise ValueError('Earlier carry grant evidence changed.')
        qty=Fraction(old.quantity_canonical)
        if old.grant_kind==grant_kind and old.grant_id==grant_id:grant_used+=qty
        if old.work_date==work_date:day_used+=qty
        if old.usage_ledger_id==usage_ledger_id:row_used+=qty
        bindings.append([old.id,old.source_digest,old.result_digest])
    if grant_used+amount>Fraction(grant['ceiling']):raise ValueError('Attribution exceeds the remaining grant ceiling.')
    if day_used+amount>1:raise ValueError('Register day already attributed across grants.')
    if row_used+amount>Fraction(usage['amount']):raise ValueError('Monthly usage debit already fully attributed.')
    return dict(grant_kind=grant_kind,grant_id=grant_id,employee_id=employee_id,leave_type_id=leave_type_id,
        monthly_receipt_id=monthly_receipt_id,usage_ledger_id=usage_ledger_id,work_date=work_date.isoformat(),
        quantity=canonical_amount(amount),reason=reason.strip(),grant=grant,usage=usage,
        prior_attributions=bindings,grant_attributed_after=canonical_amount(grant_used+amount),
        grant_unattributed_ceiling_after=canonical_amount(Fraction(grant['ceiling'])-grant_used-amount))


@bounded_workbook_operation
def review_carry_consumption(db, **selection):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean carry consumption transaction required.')
    identity={k:selection.pop(k) for k in ('actor_id','session_id','session_version','expected_epoch')}
    _locks(db,identity)
    db.execute(text('LOCK TABLE leave_carry_consumptions IN SHARE ROW EXCLUSIVE MODE'))
    payload=_derive(db,identity=identity,**selection)
    return dict(plan=payload,reviewed_digest=digest(payload),posting_allowed=False)


@bounded_workbook_operation
def record_carry_consumption(db, *, operation_id,reviewed_digest,human_confirmed,**selection):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean carry consumption transaction required.')
    if (not isinstance(operation_id,str) or re.fullmatch('[0-9a-f]{64}',operation_id) is None
            or not isinstance(reviewed_digest,str) or re.fullmatch('[0-9a-f]{64}',reviewed_digest) is None
            or human_confirmed is not True):raise ValueError('Explicit reviewed carry consumption operation required.')
    identity={k:selection.pop(k) for k in ('actor_id','session_id','session_version','expected_epoch')}
    actor=_locks(db,identity)
    db.execute(text('LOCK TABLE leave_carry_consumptions IN SHARE ROW EXCLUSIVE MODE'))
    old=db.get(LeaveCarryConsumption,operation_id,populate_existing=True)
    if old is not None:
        request,result=_verify_recorded(db,old)
        normalized={k:v.isoformat() if isinstance(v,date) else canonical_amount(v) if k=='quantity' else v for k,v in selection.items()}
        if (request['selection']!=normalized or request['identity']!=identity or request['actor_username']!=actor.username
                or old.source_digest!=reviewed_digest):raise ValueError('Carry consumption retry identity changed.')
        # Current usage/grant proof must still hold, but later attribution growth
        # must not make an honest retry exceed the already-accounted cap.
        plan=result['plan']
        if _grant(db,grant_kind=old.grant_kind,grant_id=old.grant_id,employee_id=old.employee_id,leave_type_id=old.leave_type_id,work_date=old.work_date,identity=identity)!=plan['grant']:
            raise ValueError('Recorded carry grant changed.')
        if _usage(db,monthly_receipt_id=plan['monthly_receipt_id'],usage_ledger_id=old.usage_ledger_id,
                employee_id=old.employee_id,leave_type_id=old.leave_type_id,work_date=old.work_date,identity=identity)!=plan['usage']:
            raise ValueError('Recorded carry usage changed.')
        return result
    payload=_derive(db,identity=identity,**selection)
    if digest(payload)!=reviewed_digest:raise ValueError('Carry attribution sources changed; review again.')
    normalized={k:v.isoformat() if isinstance(v,date) else canonical_amount(v) if k=='quantity' else v for k,v in selection.items()}
    request=dict(plan=payload,reviewed_digest=reviewed_digest,selection=normalized,identity=identity,actor_username=actor.username)
    result=dict(plan=payload,ledger_delta='0/1',cash_payment_committed=False)
    with db.begin_nested():
        row=LeaveCarryConsumption(id=operation_id,employee_id=payload['employee_id'],leave_type_id=payload['leave_type_id'],
            grant_kind=payload['grant_kind'],grant_id=payload['grant_id'],usage_ledger_id=payload['usage_ledger_id'],
            work_date=selection['work_date'],quantity_canonical=payload['quantity'],source_digest=reviewed_digest,
            request_json=canonical(request),request_digest=digest(request),result_json=canonical(result),result_digest=digest(result))
        db.add(row);db.add(AuditLog(actor=actor.username,action='leave_carry_consumed',entity_type='leave_carry_consumption',
            entity_id=operation_id,old_value=reviewed_digest,new_value=row.result_json,reason=payload['reason']));db.flush()
    return json.loads(row.result_json)


def refuse_unresolved_expired_carry_cash(db, *, employee_id,leave_type_id,evaluated_on,identity):
    """An expired grant still in the ledger is NOT automatically cashable.

    Until explicit expiry/exclusion accounting is posted, refuse rather than
    subtract an assumed amount or let an all-time balance monetize old days.
    Fully attributed grants have no unconsumed expiry exposure. Unattributed
    usage is never guessed to have consumed a grant.
    """
    from app.models import LeaveCarryAllocation
    from app.models import CAIRO_TZ
    evaluated_on=max(evaluated_on,datetime.now(CAIRO_TZ).date())
    grants=[]
    for kind,model in [('carry',LeaveCarryAllocation),('annual',LeaveAnnualSettlement)]:
        for row in db.scalars(select(model).where(model.employee_id==employee_id,model.leave_type_id==leave_type_id)).all():
            saved=json.loads(row.result_json)
            plan=saved['plan'] if kind=='annual' else saved
            amount=Fraction(plan['annual']['carried_days'] if kind=='annual' else plan['carried_amount'])
            if amount<=0:continue
            if kind=='annual' and plan['annual']['late_year_preservation']:
                end=date(row.source_year+1,12,31)
            else:end=date.fromisoformat(plan['available_through'])
            if evaluated_on>end:grants.append((kind,row.id,end,amount))
    for kind,grant_id,end,amount in grants:
        grant=_grant(db,grant_kind=kind,grant_id=grant_id,employee_id=employee_id,leave_type_id=leave_type_id,work_date=end,identity=identity)
        consumed=Fraction()
        for row in db.scalars(select(LeaveCarryConsumption).where(LeaveCarryConsumption.employee_id==employee_id,
                LeaveCarryConsumption.leave_type_id==leave_type_id,LeaveCarryConsumption.grant_kind==kind,
                LeaveCarryConsumption.grant_id==grant_id)).all():
            _request,saved=_verify_recorded(db,row);plan=saved['plan']
            if plan['grant']!=grant or _usage(db,monthly_receipt_id=plan['monthly_receipt_id'],usage_ledger_id=row.usage_ledger_id,
                    employee_id=employee_id,leave_type_id=leave_type_id,work_date=row.work_date,identity=identity)!=plan['usage']:
                raise ValueError('Expired carry consumption evidence changed.')
            consumed+=Fraction(row.quantity_canonical)
        if consumed>amount:raise ValueError('Expired grant consumption exceeds grant proof.')
        if consumed<amount:
            raise ValueError('Expired carry remains unresolved; cash requires reviewed expiry or exclusion accounting.')


def _refuse_unallocated_later_cash(db, *, grant_kind,grant_id,employee_id,leave_type_id):
    """A later cash debit has no grant allocation, so cannot be spent twice.

    This is reconciliation refusal, not an inferred FIFO allocation of cash.
    Cash already present in the grant's immutable source proof is not later.
    """
    from app.models import LeaveCarryAllocation
    model=LeaveAnnualSettlement if grant_kind=='annual' else LeaveCarryAllocation
    row=db.get(model,grant_id,populate_existing=True)
    saved=json.loads(row.result_json)
    source=saved['plan']['annual']['source']['source'] if grant_kind=='annual' else saved['source']['source']
    source_ids={entry[0] for entry in source['rows']}
    from app.leave_cash_out import _verify_cash_receipt
    for cash in db.scalars(select(LeaveCashOut).where(LeaveCashOut.employee_id==employee_id,LeaveCashOut.leave_type_id==leave_type_id)).all():
        _verify_cash_receipt(db,cash)
        if cash.ledger_id in source_ids or (grant_kind=='annual' and cash.id==row.cash_out_id):continue
        raise ValueError('Later cash-out lacks grant allocation; carry usage attribution requires reviewed reconciliation.')
