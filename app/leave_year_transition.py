"""Immutable completed-year checkpoint, never a second opening or spend grant.

All three existing day-balance categories must have a completed-year source and
an explicit closing receipt. No date change, cash payment, expiry, month close,
new-year earning admission or grant consumption is performed by this service.
"""
from datetime import date
import json
import re
from sqlalchemy import select, text
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_month_earning import _locks, canonical, digest, CATEGORIES
from app.leave_exact_amount import canonical_amount, exact_sum, ledger_amount
from app.leave_completed_year_source import review_completed_year_source
from app.models import (AuditLog, LeaveAnnualSettlement, LeaveBalanceTransaction,
                        LeaveCarryAllocation, LeaveCashOut, LeaveYearTransition)


def _closing_receipt(db, *, employee_id, leave_type_id, source_year, reviewed_on, identity):
    annual = db.scalar(select(LeaveAnnualSettlement).where(
        LeaveAnnualSettlement.employee_id==employee_id, LeaveAnnualSettlement.leave_type_id==leave_type_id,
        LeaveAnnualSettlement.source_year==source_year))
    carry = db.scalar(select(LeaveCarryAllocation).where(
        LeaveCarryAllocation.employee_id==employee_id, LeaveCarryAllocation.leave_type_id==leave_type_id,
        LeaveCarryAllocation.source_year==source_year))
    from app.models import LeaveZeroDecision
    zero=db.scalar(select(LeaveZeroDecision).where(LeaveZeroDecision.employee_id==employee_id,
        LeaveZeroDecision.leave_type_id==leave_type_id,LeaveZeroDecision.source_year==source_year))
    if zero is not None:
        if annual is not None or carry is not None:raise ValueError('Conflicting year closing receipts.')
        from app.leave_zero_decision import _verify
        _verify(db,zero)
        return ['zero',zero.id,zero.source_digest,zero.result_digest]
    if (annual is None)==(carry is None):
        raise ValueError('Exactly one explicit closing receipt per balance category required.')
    if carry is not None:
        from app.leave_carry_availability import read_carry_availability
        read_carry_availability(db,allocation_id=carry.id,employee_id=employee_id,
            leave_type_id=leave_type_id,work_date=reviewed_on,**identity)
        return ['carry',carry.id,carry.source_digest,carry.result_digest]
    from app.leave_annual_settlement_store import verify_annual_cash_allocation, _verify_plan_rule
    if annual.cash_out_id:
        cash=db.get(LeaveCashOut,annual.cash_out_id,populate_existing=True)
        if cash is None:raise ValueError('Annual closing cash receipt missing.')
        verify_annual_cash_allocation(db,annual,cash)
    else:
        result=json.loads(annual.result_json);request=json.loads(annual.request_json)
        audits=db.scalars(select(AuditLog).where(AuditLog.action=='leave_annual_settled',
            AuditLog.entity_type=='leave_annual_settlement',AuditLog.entity_id==annual.id)).all()
        plan=result['plan'];recorded=plan['annual']
        if (digest(result)!=annual.result_digest or digest(request)!=annual.request_digest
                or digest(plan)!=annual.source_digest or recorded['employee_id']!=employee_id
                or recorded['leave_type_id']!=leave_type_id or recorded['source_year']!=source_year
                or recorded['cash_days']!='0/1' or result['cash_result'] is not None
                or result['carry_days']!=recorded['carried_days']
                or result['cash_payment_committed'] is not False or result['carry_duplicate_credit']!='0/1'
                or len(audits)!=1 or audits[0].new_value!=annual.result_json
                or audits[0].old_value!=annual.source_digest or audits[0].actor!=request['actor_username']
                or audits[0].reason!=request['reason']):
            raise ValueError('Annual cashless closing receipt changed.')
        _verify_plan_rule(db,recorded,annual)
    return ['annual',annual.id,annual.source_digest,annual.result_digest]


def _derive(db, *, source_selections, reason):
    if not isinstance(source_selections,dict) or set(source_selections)!=set(CATEGORIES):
        raise ValueError('Explicit annual, weekly and holiday completed-year selections required.')
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=1000:
        raise ValueError('Explicit bounded year-transition reason required.')
    from app.models import LeaveType
    common=None;balances={};employee_id=None
    for code in CATEGORIES:
        selection=source_selections[code]
        fields=tuple(selection[k] for k in ('employee_id','source_year','selection_id','reviewed_on',
            'actor_id','session_id','session_version','expected_epoch'))
        if common is None:common=fields
        elif fields!=common:raise ValueError('Year-transition categories must share employee, year, source and current session.')
        kind=db.get(LeaveType,selection['leave_type_id'],populate_existing=True)
        if kind is None or kind.code!=CATEGORIES[code]:raise ValueError('Canonical year-transition category required.')
        source=review_completed_year_source(db,**selection)
        employee_id=selection['employee_id'];year=selection['source_year']
        identity={k:selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
        closing=_closing_receipt(db,employee_id=employee_id,leave_type_id=kind.id,
            source_year=year,reviewed_on=selection['reviewed_on'],identity=identity)
        rows=db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id==employee_id,
            LeaveBalanceTransaction.leave_type_id==kind.id).order_by(LeaveBalanceTransaction.id)).all()
        balances[code]=dict(leave_type_id=kind.id,source_digest=source['source_digest'],source=source['source'],
            closing_receipt=closing,year_amount=canonical_amount(source['amount']),held_annual=canonical_amount(source['held_annual']),
            all_time_balance=canonical_amount(exact_sum(ledger_amount(row) for row in rows)),
            ledger=[(row.id,canonical_amount(ledger_amount(row)),row.source_operation_key,row.source_digest,
                     row.earning_year,row.released_on.isoformat() if row.released_on else None) for row in rows])
    return dict(employee_id=employee_id,source_year=year,target_year=year+1,selection_id=common[2],
        reviewed_on=common[3].isoformat(),balances=balances,reason=reason.strip(),ledger_delta='0/1',
        new_opening_credit='0/1',new_year_earning_allowed=False,operational_spending_allowed=False)


@bounded_workbook_operation
def review_year_transition(db, *, source_selections, reason):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean year-transition transaction required.')
    payload=_derive(db,source_selections=source_selections,reason=reason)
    return dict(plan=payload,reviewed_digest=digest(payload),posting_allowed=False)


@bounded_workbook_operation
def record_year_transition(db, *, operation_id, reviewed_digest, human_confirmed, source_selections, reason):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean year-transition transaction required.')
    if (not isinstance(operation_id,str) or re.fullmatch('[0-9a-f]{64}',operation_id) is None
            or not isinstance(reviewed_digest,str) or re.fullmatch('[0-9a-f]{64}',reviewed_digest) is None
            or human_confirmed is not True):raise ValueError('Explicit confirmed year-transition operation required.')
    if not isinstance(source_selections,dict) or set(source_selections)!=set(CATEGORIES):
        raise ValueError('Explicit annual, weekly and holiday completed-year selections required.')
    first=source_selections['A']
    identity={k:first[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    actor=_locks(db,identity)
    db.execute(text('LOCK TABLE leave_year_transitions,leave_annual_settlements,leave_carry_allocations IN SHARE ROW EXCLUSIVE MODE'))
    old=db.get(LeaveYearTransition,operation_id,populate_existing=True)
    if old is not None:
        # Lost-response retry: verify the STORED evidence against the reviewed digest and this request's identity,
        # without re-deriving from a ledger that may have grown since (re-deriving would refuse every late retry).
        stored=json.loads(old.result_json);stored_request=json.loads(old.request_json)
        audits=db.scalars(select(AuditLog).where(AuditLog.action=='leave_year_transition_recorded',
            AuditLog.entity_type=='leave_year_transition',AuditLog.entity_id==old.id)).all()
        if (old.result_digest!=reviewed_digest or digest(stored)!=reviewed_digest or old.source_digest!=reviewed_digest
                or old.result_json!=canonical(stored) or old.request_json!=canonical(stored_request)
                or old.request_digest!=digest(stored_request) or stored_request['plan']!=stored
                or stored_request['reviewed_digest']!=reviewed_digest or stored_request['actor_username']!=actor.username
                or any(stored_request[k]!=v for k,v in identity.items())
                or stored['employee_id']!=old.employee_id or stored['source_year']!=old.source_year
                or stored['target_year']!=old.target_year or stored['employee_id']!=first['employee_id']
                or stored['source_year']!=first['source_year'] or stored['reason']!=reason.strip()
                or len(audits)!=1 or audits[0].actor!=actor.username or audits[0].old_value!=reviewed_digest
                or audits[0].new_value!=old.result_json or audits[0].reason!=reason.strip()):
            raise ValueError('Year-transition recorded evidence changed.')
        return stored
    payload=_derive(db,source_selections=source_selections,reason=reason)
    if digest(payload)!=reviewed_digest:raise ValueError('Year-transition sources changed; review again.')
    request=dict(plan=payload,reviewed_digest=reviewed_digest,actor_username=actor.username,**identity)
    if db.scalar(select(LeaveYearTransition.id).where(LeaveYearTransition.employee_id==payload['employee_id'],
            LeaveYearTransition.source_year==payload['source_year'])):
        raise ValueError('Source year already has a transition checkpoint.')
    with db.begin_nested():
        row=LeaveYearTransition(id=operation_id,employee_id=payload['employee_id'],source_year=payload['source_year'],
            target_year=payload['target_year'],source_digest=reviewed_digest,request_json=canonical(request),
            request_digest=digest(request),result_json=canonical(payload),result_digest=reviewed_digest)
        db.add(row);db.add(AuditLog(actor=actor.username,action='leave_year_transition_recorded',
            entity_type='leave_year_transition',entity_id=operation_id,old_value=reviewed_digest,
            new_value=row.result_json,reason=reason.strip()));db.flush()
    return json.loads(row.result_json)


def _selection_epoch(db,selection_id):
    from app.models import LeaveOpeningSelection
    row=db.get(LeaveOpeningSelection,selection_id,populate_existing=True)
    return None if row is None else row.restore_epoch


def verify_transition_for_month(db, *, employee_id, selection_id, month_start, identity):
    """Verify every intervening checkpoint and original monthly source evidence.

    This admits monthly accrual/register accounting only. It does not grant carry
    spending, erase expired days, or enable the completed-year cash service.
    """
    from app.models import LeaveMonthEarning, Employee
    from app.leave_month_earning import _verify_receipt, _earning_binding
    from app.department_workbook_source import read_workbook_source
    from app.department_workbook_tiers import resolve_workbook_tiers
    from app.leave_opening_source import read_confirmed_opening_source
    from app.leave_opening_receipt import verified_manual_opening
    reviews,policy,policy_digest=read_confirmed_opening_source(db,selection_id=selection_id,**identity)
    opening=verified_manual_opening(db,selection_id=selection_id,**identity)
    from app.leave_opening_rebind import lineage_ids
    allowed=lineage_ids(db,selection_id)
    baseline=date.fromisoformat(reviews[0]['effective_start'])
    if baseline.year>=month_start.year:raise ValueError('Earlier baseline required for transition admission.')
    employee=db.get(Employee,employee_id,populate_existing=True)
    bindings=[]
    for year in range(baseline.year,month_start.year):
        row=db.scalar(select(LeaveYearTransition).where(LeaveYearTransition.employee_id==employee_id,
            LeaveYearTransition.source_year==year))
        if row is None:raise ValueError('Reviewed year-transition checkpoint required before next-year earning.')
        result=json.loads(row.result_json);request=json.loads(row.request_json)
        audits=db.scalars(select(AuditLog).where(AuditLog.action=='leave_year_transition_recorded',
            AuditLog.entity_type=='leave_year_transition',AuditLog.entity_id==row.id)).all()
        if (row.source_digest!=row.result_digest or digest(result)!=row.result_digest
                or digest(request)!=row.request_digest or request['plan']!=result
                or request['reviewed_digest']!=row.result_digest or request['expected_epoch']!=_selection_epoch(db,result['selection_id'])
                or result['employee_id']!=employee_id or result['source_year']!=year or result['target_year']!=year+1
                or row.target_year!=year+1 or result['selection_id'] not in allowed
                or result['new_opening_credit']!='0/1' or result['ledger_delta']!='0/1'
                or result['new_year_earning_allowed'] is not False or result['operational_spending_allowed'] is not False
                or len(audits)!=1 or audits[0].actor!=request['actor_username']
                or audits[0].old_value!=row.source_digest or audits[0].new_value!=row.result_json
                or audits[0].reason!=result['reason']):
            raise ValueError('Year-transition checkpoint integrity changed.')
        for code,balance in result['balances'].items():
            # Existing named rows cannot be changed merely because later rows grow.
            for item in balance['ledger']:
                ledger=db.get(LeaveBalanceTransaction,item[0],populate_existing=True)
                actual=[ledger.id,canonical_amount(ledger_amount(ledger)),ledger.source_operation_key,
                    ledger.source_digest,ledger.earning_year,ledger.released_on.isoformat() if ledger.released_on else None] if ledger else None
                if (actual!=item or ledger.employee_id!=employee_id or ledger.leave_type_id!=balance['leave_type_id']):
                    raise ValueError('Year-transition recorded ledger changed.')
            closing=_closing_receipt(db,employee_id=employee_id,leave_type_id=balance['leave_type_id'],
                source_year=year,reviewed_on=date.fromisoformat(result['reviewed_on']),identity=identity)
            if closing!=balance['closing_receipt']:raise ValueError('Year-transition closing receipt changed.')
        receipts=db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id==employee_id,
            LeaveMonthEarning.month_start>=date(year,1,1),LeaveMonthEarning.month_start<=date(year,12,1))).all()
        expected={item[0]:item[1:] for item in result['balances']['A']['source']['verified_months']}
        if set(expected)!={receipt.id for receipt in receipts}:raise ValueError('Transition source-month coverage changed.')
        for receipt in receipts:
            _verify_receipt(db,receipt)
            if expected[receipt.id]!=[receipt.source_digest,receipt.result_digest] or receipt.selection_id not in allowed:
                raise ValueError('Transition monthly receipt changed.')
            old=json.loads(receipt.source_json)
            facts=read_workbook_source(db,department_id=employee.department_id,employee_ids=(employee_id,),
                window_start=date(year,1,1),month_start=receipt.month_start,policy_digest=policy_digest,
                expected_epoch=identity['expected_epoch'])
            if old['earning_binding']!=_earning_binding(json.loads(facts.canonical_json),receipt.month_start):
                raise ValueError('Transition prior earned source changed.')
            insurance={(i,date.fromisoformat(m)):None if v is None else __import__('decimal').Decimal(v)
                for i,m,v in old['current_insurance_years']}
            ages={date.fromisoformat(m):date.fromisoformat(d) for m,d in old['age_evaluation_dates']}
            tiers=resolve_workbook_tiers(facts=facts,policy=policy,current_insurance_years=insurance,age_evaluation_dates=ages)
            if (tiers.unresolved or tiers.tiers[employee_id,receipt.month_start]!=old['tier']
                    or old['opening']!=opening['apply_sha256']):raise ValueError('Transition prior tier or opening changed.')
        bindings.append([row.id,row.source_digest,row.result_digest])
    return bindings
