"""Exact source-bound month earning and register usage, caller-owned transaction.

No inferred opening, no forecast posting, no year close/carry/cash workflow.
Held annual is an immutable receipt amount, never a spendable ledger credit.
"""
import json
import re
from calendar import monthrange
from datetime import date, datetime
from fractions import Fraction
from hashlib import sha256

from sqlalchemy import select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_source import read_workbook_source
from app.department_workbook_store import _guard
from app.department_workbook_tiers import resolve_workbook_tiers
from app.leave_day_calculations import employed_dates
from app.leave_exact_amount import canonical_amount, ledger_amount, storage_amount
from app.leave_exact_month import calculate_exact_month
from app.leave_opening_receipt import verified_manual_opening
from app.leave_opening_source import read_confirmed_opening_source
from app.leave_release_preview import annual_release_preview
from app.models import (
    CAIRO_TZ,
    AuditLog,
    Employee,
    LeaveBalanceTransaction,
    LeaveMonthClose,
    LeaveMonthEarning,
    LeaveRecord,
    LeaveType,
    PayrollRun,
)

CATEGORIES={'A':'annual','W':'weekly_rest','H':'official_holiday'}


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def _locks(db,identity):
    if db.get_bind().dialect.name != 'postgresql':
        raise ValueError('Exact monthly posting requires migrated PostgreSQL.')
    actor=_guard(db,**identity)
    db.execute(text('LOCK TABLE audit_logs,employees,leave_balance_transactions,leave_month_earnings,leave_month_close,payroll_runs IN SHARE ROW EXCLUSIVE MODE'))
    db.execute(text('LOCK TABLE leave_opening_selections,leave_manual_opening_groups,leave_historical_evidence_preparations,leave_policy_versions,leave_records IN SHARE MODE'))
    return actor


def _validate(month_start,evaluated_on,selection_id):
    if (type(month_start) is not date or month_start.day!=1 or month_start.year==9999
            or type(evaluated_on) is not date
            or evaluated_on < date(month_start.year,month_start.month,monthrange(month_start.year,month_start.month)[1])
            or evaluated_on>datetime.now(CAIRO_TZ).date()
            or not isinstance(selection_id,str) or re.fullmatch('[0-9a-f]{64}',selection_id) is None):
        raise ValueError('Explicit completed calendar month, current evaluation date and selection required.')


def _earning_binding(source,month):
    end=date(month.year,month.month,monthrange(month.year,month.month)[1])
    return digest({'employees': [{k:e[k] for k in ('id','hire_date','birth_date','prior_insurance_years')} for e in source['employees']],'service': [p for p in source['service_periods'] if date.fromisoformat(p['start_date'])<=end and (p['end_date'] is None or date.fromisoformat(p['end_date'])>=month)],
        'register': [r for r in source['register'] if month<=date.fromisoformat(r['work_date'])<=end],
        'holidays': [h for h in source['holidays'] if month<=date.fromisoformat(h['holiday_date'])<=end]})


def _derive(db,*,selection_id,month_start,evaluated_on,current_insurance_years,age_evaluation_dates,**identity):
    _validate(month_start,evaluated_on,selection_id)
    _locks(db,identity)
    reviews,policy,source_digest=read_confirmed_opening_source(db,selection_id=selection_id,**identity)
    verified=verified_manual_opening(db,selection_id=selection_id,**identity)
    employee_id=verified['employee_id']
    employee=db.get(Employee,employee_id,populate_existing=True)
    effective={r['effective_start'] for r in reviews}
    if len(effective)!=1:raise ValueError('One explicit baseline required.')
    baseline=date.fromisoformat(next(iter(effective)))
    end=date(month_start.year,month_start.month,monthrange(month_start.year,month_start.month)[1])
    transition_bindings=[]
    if baseline.year<month_start.year:
        from app.leave_year_transition import verify_transition_for_month
        transition_bindings=verify_transition_for_month(db,employee_id=employee_id,selection_id=selection_id,month_start=month_start,identity=identity)
    if baseline.year>month_start.year or baseline>end or policy.annual_year_basis!='calendar':
        raise ValueError('Current calendar baseline must cover selected month.')
    if db.scalar(select(LeaveMonthClose.id).where(LeaveMonthClose.year==month_start.year,LeaveMonthClose.month==month_start.month)):
        raise ValueError('Closed leave month refuses earning.')
    if db.scalar(select(PayrollRun.id).where(PayrollRun.period_start<=end,PayrollRun.period_end>=month_start,PayrollRun.status.in_(('approved','closed')))):
        raise ValueError('Approved/closed payroll refuses earning.')
    facts=read_workbook_source(db,department_id=employee.department_id,employee_ids=(employee_id,),
        window_start=date(month_start.year,1,1),month_start=month_start,policy_digest=source_digest,expected_epoch=identity['expected_epoch'])
    source=json.loads(facts.canonical_json)
    tiers=resolve_workbook_tiers(facts=facts,policy=policy,current_insurance_years=current_insurance_years,age_evaluation_dates=age_evaluation_dates)
    if tiers.unresolved:raise ValueError('Resolve explicit age/insurance tier evidence before earning.')
    periods=[(date.fromisoformat(p['start_date']),date.fromisoformat(p['end_date']) if p['end_date'] else None) for p in source['service_periods']]
    if not periods:raise ValueError('Explicit service evidence required.')
    for n in range(end.day):
        day=month_start.fromordinal(month_start.toordinal()+n)
        if sum(start<=day and (finish is None or finish>=day) for start,finish in periods)>1:
            raise ValueError('Overlapping service evidence refuses earning.')
    employed=employed_dates(max(month_start,baseline),end,periods)
    if not employed:raise ValueError('No employed days in selected month.')
    if any(day<employee.hire_date for day in employed):raise ValueError('Service before recorded hire conflicts.')
    entries={date.fromisoformat(r['work_date']):r for r in source['register']}
    if any(day not in entries or entries[day]['code'] not in {'','A','W','H','S','U'} for day in employed):
        raise ValueError('Complete explicit register required; unknown days are not presence.')
    if any(month_start<=day<=end and day not in employed and day>=baseline and r['code'] not in {None,'—'} for day,r in entries.items()):
        raise ValueError('Register outside service conflicts.')
    counts={code:sum(entries[d]['code']==code for d in employed) for code in ('U','S','A','W','H')}
    holiday_dates={date.fromisoformat(r['holiday_date']) for r in source['holidays']}
    holidays={d for d in holiday_dates & employed if policy.holiday_credit_during_sick or entries[d]['code']!='S'}
    earned=calculate_exact_month(tier=tiers.tiers[employee_id,month_start],days_in_month=end.day,
        employed_days=len(employed),unpaid_days=counts['U'],sick_days=counts['S'],holiday_days=len(holidays),
        annual_deduct_unpaid=policy.annual_deduct_unpaid_absence,annual_deduct_sick=policy.annual_deduct_sick)
    kinds={}
    for code,host in CATEGORIES.items():
        matches=db.scalars(select(LeaveType).where(LeaveType.code==host)).all()
        if len(matches)!=1 or not matches[0].active or not matches[0].tracks_balance:
            raise ValueError('Canonical active balance-tracking categories required.')
        kinds[code]=matches[0].id
    # A/W/H register facts have not implicitly spent ledger. Post explicit per-month
    # usage only if no unrelated legacy leave/debit may represent the same days.
    ledger=db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id==employee_id,
        LeaveBalanceTransaction.leave_type_id.in_(kinds.values()))).all()
    existing_receipts=db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id==employee_id)).all()
    known_ledger=set(verified['ledger_codes'])
    for receipt in existing_receipts:
        verified_result=_verify_receipt(db,receipt)
        known_ledger.update(item['id'] for item in verified_result['ledger'])
        if receipt.month_start.year==month_start.year and json.loads(receipt.source_json)['earning_binding']!=_earning_binding(source,receipt.month_start):
            raise ValueError('Prior earned source changed; separate correction required before release.')
    release_audits=db.scalars(select(AuditLog).where(AuditLog.action=='leave_annual_released',AuditLog.entity_type=='leave_annual_release')).all()
    for audit in release_audits:
        try:
            body=json.loads(audit.new_value)
            if not isinstance(body,dict) or type(body.get('employee_id')) is not int:raise ValueError
            owned=body['employee_id']==employee_id
            items=body["ledger"] if owned else []
            if not isinstance(items,list) or any(not isinstance(i,dict) or type(i.get('id')) is not int
                    or not isinstance(i.get('key'),str) or not isinstance(i.get('amount'),str)
                    or type(i.get('leave_type_id')) is not int for i in items):raise ValueError
        except (TypeError,KeyError,ValueError) as exc:
            raise ValueError('Malformed annual release audit evidence (audit id %s); refusing, not ignored.'%audit.id) from exc
        if owned:
            for item in items:
                released_row=db.get(LeaveBalanceTransaction,item['id'],populate_existing=True)
                if (released_row is None or released_row.source_operation_key!=item['key']
                        or canonical_amount(ledger_amount(released_row))!=item['amount']
                        or released_row.employee_id!=employee_id or released_row.leave_type_id!=item['leave_type_id']):
                    raise ValueError('Prior annual release result changed.')
                known_ledger.add(item['id'])
    # A verified manual/annual cash-out debit is a known, receipt-proven reduction of a released balance,
    # not an unrelated legacy debit that could represent the same register days.
    from app.leave_cash_out import _verify_cash_receipt
    from app.models import LeaveCashOut
    for cash_receipt in db.scalars(select(LeaveCashOut).where(LeaveCashOut.employee_id==employee_id)).all():
        _verify_cash_receipt(db,cash_receipt)
        known_ledger.add(cash_receipt.ledger_id)
    for row in ledger:
        if row.id not in known_ledger:
            raise ValueError('Existing unrelated ledger requires separate reviewed reconciliation.')
    if db.scalar(select(LeaveRecord.id).where(LeaveRecord.employee_id==employee_id,LeaveRecord.leave_type_id.in_(kinds.values()),LeaveRecord.status!='cancelled')):
        raise ValueError('Existing leave records require separate overlap reconciliation.')
    initial=annual_release_preview(hire_date=employee.hire_date,as_of=evaluated_on,full_calendar_month_worked=None)
    first_end=date.fromordinal(initial.earliest_release_date.toordinal()-1)
    first_start=first_end.replace(day=1)
    service_full=employed_dates(first_start,first_end,periods)
    released=annual_release_preview(hire_date=employee.hire_date,as_of=evaluated_on,
        full_calendar_month_worked=len(service_full)==first_end.day)
    previous=db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id==employee_id).order_by(LeaveMonthEarning.month_start)).all()
    expected_month=max(baseline.replace(day=1),month_start.replace(month=1))
    prior_months=[]
    while expected_month<month_start:
        prior_months.append(expected_month)
        expected_month=date(expected_month.year+int(expected_month.month==12),expected_month.month%12+1,1)
    if {r.month_start for r in previous if r.month_start.year==month_start.year and r.month_start<month_start}!=set(prior_months):
        raise ValueError('Post source months sequentially from explicit baseline; no omitted prior month.')
    if any(r.selection_id!=selection_id for r in previous):raise ValueError('Monthly baseline changed; reviewed correction required.')
    payload={'employee_id': employee_id,'selection_id': selection_id,'month_start': month_start.isoformat(),'evaluated_on': evaluated_on.isoformat(),
        'baseline': baseline.isoformat(),'source': facts.digest,'opening': verified['apply_sha256'],'earning_binding': _earning_binding(source,month_start),
        'tier': tiers.tiers[employee_id,month_start],'age_evaluation_dates': sorted((m.isoformat(),d.isoformat()) for m,d in age_evaluation_dates.items()),
        'current_insurance_years': sorted((i,m.isoformat(),str(v) if v is not None else None) for (i,m),v in current_insurance_years.items()),
        'earned': {c:canonical_amount(v) for c,v in zip(('A','W','H'),(earned.annual,earned.weekly,earned.holiday))},
        'used': {c:counts[c] for c in CATEGORIES},'annual_released': released.released,'annual_release_date': initial.earliest_release_date.isoformat(),
        'previous': [(r.id,r.source_digest,r.result_digest) for r in previous]}
    if transition_bindings:payload['year_transitions']=transition_bindings
    return payload,kinds


@bounded_workbook_operation
def review_month_earning(db,**selection):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean transaction required.')
    payload,_kinds=_derive(db,**selection)
    return {'plan':payload,'reviewed_digest':digest(payload),'posted':False}


def _verify_receipt(db,row):
    if digest(json.loads(row.source_json))!=row.source_digest or digest(json.loads(row.result_json))!=row.result_digest:
        raise ValueError('Monthly receipt integrity mismatch.')
    result=json.loads(row.result_json)
    for item in result['ledger']:
        ledger=db.get(LeaveBalanceTransaction,item['id'],populate_existing=True)
        if ledger is None or ledger.source_operation_key!=item['key'] or ledger.source_digest!=row.source_digest:
            raise ValueError('Monthly ledger result changed.')
        if (ledger.employee_id!=row.employee_id or ledger.leave_type_id!=item['leave_type_id']
                or canonical_amount(ledger_amount(ledger))!=item['amount'] or ledger.transaction_type!='adjustment'
                or ledger.earning_year!=date.fromisoformat(item['earning_month']).year
                or ledger.released_on!=date.fromisoformat(item['released_on'])):
            raise ValueError('Monthly ledger amount or identity changed.')
    audit=db.scalars(select(AuditLog).where(AuditLog.action=='leave_month_earned',AuditLog.entity_type=='leave_month_earning',AuditLog.entity_id==row.id)).all()
    if len(audit)!=1 or audit[0].new_value!=row.result_json or audit[0].old_value!=row.source_digest:
        raise ValueError('Monthly audit proof changed.')
    return result


@bounded_workbook_operation
def apply_month_earning(db,*,operation_id,reviewed_digest,human_confirmed,reason,**selection):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean transaction required.')
    if (not isinstance(operation_id,str) or re.fullmatch('[0-9a-f]{64}',operation_id) is None
            or not isinstance(reviewed_digest,str) or re.fullmatch('[0-9a-f]{64}',reviewed_digest) is None
            or human_confirmed is not True or not isinstance(reason,str) or not reason.strip() or len(reason)>1000):
        raise ValueError('Explicit reviewed operation, confirmation and bounded reason required.')
    identity={k:selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    actor=_locks(db,identity)
    _validate(selection['month_start'],selection['evaluated_on'],selection['selection_id'])
    request={**selection,'current_insurance_years':sorted((i,m.isoformat(),str(v) if v is not None else None) for (i,m),v in selection['current_insurance_years'].items()),
        'age_evaluation_dates':sorted((m.isoformat(),d.isoformat()) for m,d in selection['age_evaluation_dates'].items())}
    for key in ('month_start','evaluated_on'):request[key]=request[key].isoformat()
    request_digest=digest({'selection': request,'reviewed_digest': reviewed_digest,'reason': reason.strip()})
    existing=db.get(LeaveMonthEarning,operation_id,populate_existing=True)
    if existing is not None:
        if (existing.request_digest!=request_digest or existing.actor_id!=actor.id
                or existing.session_id!=identity['session_id'] or existing.session_version!=identity['session_version']
                or existing.restore_epoch!=identity['expected_epoch']):
            raise ValueError('Monthly operation identity changed.')
        return _verify_receipt(db,existing)
    from app.models import LeaveOpeningSelection
    current_selection=db.get(LeaveOpeningSelection,selection['selection_id'],populate_existing=True)
    if current_selection is not None and db.scalar(select(LeaveMonthEarning.id).where(
            LeaveMonthEarning.employee_id==current_selection.employee_id,
            LeaveMonthEarning.month_start==selection['month_start'])):
        raise ValueError('Monthly source already posted; reviewed correction required.')
    payload,kinds=_derive(db,**selection)
    source_digest=digest(payload)
    if source_digest!=reviewed_digest:raise ValueError('Monthly evidence changed; review again.')
    if db.scalar(select(LeaveMonthEarning.id).where(LeaveMonthEarning.employee_id==payload['employee_id'],LeaveMonthEarning.month_start==selection['month_start'])):
        raise ValueError('Monthly source already posted; reviewed correction required.')
    previous=db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id==payload['employee_id']).order_by(LeaveMonthEarning.month_start)).all()
    prior_results=[(row,_verify_receipt(db,row)) for row in previous]
    released_months={item['earning_month'] for _row,result in prior_results for item in result['ledger'] if item['category']=='A' and item['kind']=='earned'}
    for key in db.scalars(select(LeaveBalanceTransaction.source_operation_key).where(LeaveBalanceTransaction.employee_id==payload['employee_id'])):
        if key and key.startswith('month_earning:') and key.endswith(':A:earned'):
            released_months.add(key.split(':')[2])
    credits=[]
    month=selection['month_start'].isoformat()
    for code in CATEGORIES:
        amount=Fraction(payload['earned'][code])
        if code!='A' or payload['annual_released']:
            credits.append((code,amount,month,'earned'))
        credits.append((code,Fraction(-payload['used'][code]),month,'usage'))
    if payload['annual_released']:
        for old,old_result in prior_results:
            oldmonth=old.month_start.isoformat()
            if oldmonth not in released_months:
                held=Fraction(json.loads(old.source_json)['earned']['A'])
                credits.append(('A',held,oldmonth,'earned'))
    result={'ledger':[],'held_annual':payload['earned']['A'] if not payload['annual_released'] else '0/1'}
    with db.begin_nested():
        for code,amount,earning_month,kind in credits:
            if not amount:continue
            key=f'month_earning:{payload["employee_id"]}:{earning_month}:{code}:{kind}'
            ledger=LeaveBalanceTransaction(employee_id=payload['employee_id'],leave_type_id=kinds[code],transaction_type='adjustment',
                **storage_amount(amount),reason=reason.strip(),created_by=actor.username,source_operation_key=key,source_digest=source_digest,
                earning_year=date.fromisoformat(earning_month).year,released_on=selection['evaluated_on'])
            db.add(ledger);db.flush()
            result['ledger'].append({'id': ledger.id,'key': key,'leave_type_id': kinds[code],'category': code,'kind': kind,
                'amount': canonical_amount(amount),'earning_month': earning_month,'released_on': selection['evaluated_on'].isoformat()})
        receipt=LeaveMonthEarning(id=operation_id,employee_id=payload['employee_id'],selection_id=selection['selection_id'],month_start=selection['month_start'],
            actor_id=actor.id,session_id=identity['session_id'],session_version=identity['session_version'],restore_epoch=identity['expected_epoch'],
            source_digest=source_digest,request_digest=request_digest,source_json=canonical(payload),result_json=canonical(result),result_digest=digest(result))
        db.add(receipt)
        db.add(AuditLog(actor=actor.username,action='leave_month_earned',entity_type='leave_month_earning',entity_id=operation_id,
            old_value=source_digest,new_value=canonical(result),reason=reason.strip()))
        db.flush()
    return result


@bounded_workbook_operation
def review_annual_release(db,**selection):
    """Re-evaluate held receipts after completed first full month, no new accrual."""
    if db.new or db.dirty or db.deleted:raise ValueError('Clean transaction required.')
    payload,kinds=_derive(db,**selection)
    if not payload['annual_released']:raise ValueError('Full first calendar month release evidence unresolved.')
    receipts=db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id==payload['employee_id'],LeaveMonthEarning.month_start<=selection['month_start']).order_by(LeaveMonthEarning.month_start)).all()
    if not receipts or receipts[-1].month_start!=selection['month_start']:
        raise ValueError('Post completed source month before release-only review.')
    ledger_keys=set(db.scalars(select(LeaveBalanceTransaction.source_operation_key).where(LeaveBalanceTransaction.employee_id==payload['employee_id'])))
    held=[]
    for row in receipts:
        _verify_receipt(db,row)
        month=row.month_start.isoformat();key=f'month_earning:{row.employee_id}:{month}:A:earned'
        if key not in ledger_keys:
            amount=json.loads(row.source_json)['earned']['A']
            if Fraction(amount):held.append({'receipt': row.id,'month': month,'amount': amount,'key': key,'leave_type_id': kinds['A']})
    if not held:raise ValueError('No unreleased annual earning remains.')
    plan={'source':payload,'held':held,'kind':'annual_release_only'}
    return {'plan':plan,'reviewed_digest':digest(plan),'posted':False}


@bounded_workbook_operation
def apply_annual_release(db,*,operation_id,reviewed_digest,human_confirmed,reason,**selection):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean transaction required.')
    if (not isinstance(operation_id,str) or re.fullmatch('[0-9a-f]{64}',operation_id) is None
            or not isinstance(reviewed_digest,str) or re.fullmatch('[0-9a-f]{64}',reviewed_digest) is None
            or human_confirmed is not True or not isinstance(reason,str) or not reason.strip() or len(reason)>1000):
        raise ValueError('Explicit reviewed release operation, confirmation and reason required.')
    identity={k:selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    actor=_locks(db,identity)
    existing=db.scalars(select(AuditLog).where(AuditLog.action=='leave_annual_released',AuditLog.entity_type=='leave_annual_release',AuditLog.entity_id==operation_id)).all()
    authority={'actor_id': actor.id,'session_id': identity['session_id'],'session_version': identity['session_version'],'restore_epoch': identity['expected_epoch']}
    request={'selection_id': selection['selection_id'],'month_start': selection['month_start'].isoformat(),'evaluated_on': selection['evaluated_on'].isoformat(),
        'current_insurance_years': sorted((i,m.isoformat(),str(v) if v is not None else None) for (i,m),v in selection['current_insurance_years'].items()),
        'age_evaluation_dates': sorted((m.isoformat(),d.isoformat()) for m,d in selection['age_evaluation_dates'].items()),
        'reviewed_digest': reviewed_digest,'reason': reason.strip(),'authority': authority}
    request_digest=digest(request)
    if existing:
        if len(existing)!=1 or existing[0].old_value!=request_digest:raise ValueError('Release operation identity changed.')
        result=json.loads(existing[0].new_value)
        for item in result['ledger']:
            row=db.get(LeaveBalanceTransaction,item['id'],populate_existing=True)
            if (row is None or row.source_operation_key!=item['key'] or row.source_digest!=reviewed_digest
                    or canonical_amount(ledger_amount(row))!=item['amount'] or row.employee_id!=result['employee_id']
                    or row.leave_type_id!=item['leave_type_id']):
                raise ValueError('Release ledger result changed.')
        return result
    review=review_annual_release(db,**selection)
    if review['reviewed_digest']!=reviewed_digest:raise ValueError('Release evidence changed; review again.')
    result={'employee_id':review['plan']['source']['employee_id'],'ledger':[]}
    with db.begin_nested():
        for item in review['plan']['held']:
            row=LeaveBalanceTransaction(employee_id=result['employee_id'],leave_type_id=item['leave_type_id'],transaction_type='adjustment',
                **storage_amount(Fraction(item['amount'])),reason=reason.strip(),created_by=actor.username,source_operation_key=item['key'],
                source_digest=reviewed_digest,earning_year=date.fromisoformat(item['month']).year,released_on=selection['evaluated_on'])
            db.add(row);db.flush()
            result['ledger'].append({'id': row.id,'key': item['key'],'amount': item['amount'],'leave_type_id': item['leave_type_id'],'category': 'A','kind': 'earned','earning_month': item['month']})
        db.add(AuditLog(actor=actor.username,action='leave_annual_released',entity_type='leave_annual_release',entity_id=operation_id,
            old_value=request_digest,new_value=canonical(result),reason=reason.strip()))
        db.flush()
    return result
