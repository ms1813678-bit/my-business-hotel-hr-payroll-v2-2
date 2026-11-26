"""Atomic normal annual cash-day debit/liability and remaining carry receipt.

No money transfer, payroll modification, expiry or automatic future-year credit.
The carry stays the existing exact ledger remainder, not a second positive row.
"""
from datetime import date
from fractions import Fraction
import json
import re
from sqlalchemy import select, text
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_annual_settlement_source import review_annual_settlement
from app.leave_cash_out import review_cash_out, apply_cash_out, _locks, _verify_cash_receipt
from app.leave_exact_amount import canonical_amount
from app.leave_month_earning import canonical, digest
from app.models import AuditLog, LeaveAnnualSettlement, LeaveCashOut, LeaveCarryAllocation


def _review(db, *, source_selection, full_annual_entitlement, payroll_item_id,
        available_from, available_through, reason):
    annual = review_annual_settlement(db, source_selection=source_selection,
        full_annual_entitlement=full_annual_entitlement, other_half_action='carry')
    plan = annual['plan']
    if _blocking_prior_cash(db, plan['employee_id'], plan['leave_type_id'], source_selection['source_year']):
        raise ValueError('Prior cash-out for this employee and leave type requires separate reviewed reconciliation.')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError('Explicit bounded annual reason required.')
    carry = Fraction(plan['carried_days'])
    if carry > 0:
        if (type(available_from) is not date or type(available_through) is not date
                or not source_selection['reviewed_on'] <= available_from <= available_through):
            raise ValueError('Explicit annual carry availability dates required.')
    elif available_from is not None or available_through is not None:
        raise ValueError('No positive carry; no positive availability window.')
    identity = {k: source_selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    cash_days = Fraction(plan['cash_days'])
    cash = review_cash_out(db, employee_id=plan['employee_id'], leave_type_id=plan['leave_type_id'],
        days=cash_days, payroll_item_id=payroll_item_id, evaluated_on=source_selection['reviewed_on'],
        source_year=source_selection['source_year'], **identity) if cash_days > 0 else None
    if cash is None and payroll_item_id is not None:
        raise ValueError('No cash days; no payroll basis selection required.')
    result = dict(annual=plan, cash=cash['plan'] if cash else None,
        cash_reviewed_digest=cash['reviewed_digest'] if cash else None,
        available_from=available_from.isoformat() if available_from else None,
        available_through=available_through.isoformat() if available_through else None,
        reason=reason.strip(), cash_payment_committed=False)
    return result


def _verify_plan_rule(db, annual, row):
    """Recompute the owner's annual rule from the recorded plan's own source facts; a forged carry/cash fails."""
    from app.leave_annual_settlement import annual_settlement_split
    from app.models import Employee
    from app.leave_recorded_source_versions import verify_versioned_recorded_source
    verify_versioned_recorded_source(db, result=dict(source=annual['source'], source_digest=annual['source_digest'],
        source_year=row.source_year, carried_amount=annual['released_remaining'], held_annual=annual['held_remaining']),
        employee_id=row.employee_id, leave_type_id=row.leave_type_id)
    employee = db.get(Employee, row.employee_id, populate_existing=True)
    split = annual_settlement_split(full_annual_entitlement=annual['full_annual_entitlement'],
        remaining=Fraction(annual['remaining']), hire_date=employee.hire_date, source_year=row.source_year,
        other_half_action='carry', annual_release_proven=Fraction(annual['held_remaining']) == 0)
    expected = dict(cash_days=canonical_amount(split.cash_days), carried_days=canonical_amount(split.carried_days),
        cleared_days=canonical_amount(split.cleared_days), held_days=canonical_amount(split.held_days))
    if (any(annual[k] != v for k, v in expected.items()) or annual['other_half_action'] != 'carry'
            or Fraction(annual['remaining']) != Fraction(annual['released_remaining']) + Fraction(annual['held_remaining'])):
        raise ValueError('Annual recorded plan violates the settlement rule.')


@bounded_workbook_operation
def review_annual_settlement_apply(db, **selection):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean annual transaction required.')
    payload = _review(db, **selection)
    return dict(plan=payload, reviewed_digest=digest(payload), posting_allowed=False)


@bounded_workbook_operation
def apply_annual_settlement(db, *, operation_id, reviewed_digest, human_confirmed, **selection):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean annual transaction required.')
    if (not isinstance(operation_id,str) or re.fullmatch('[0-9a-f]{64}',operation_id) is None
            or not isinstance(reviewed_digest,str) or re.fullmatch('[0-9a-f]{64}',reviewed_digest) is None
            or human_confirmed is not True):
        raise ValueError('Explicit confirmed annual review operation required.')
    source = selection['source_selection']
    identity = {k: source[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    actor = _locks(db, identity)
    db.execute(text('LOCK TABLE leave_annual_settlements,leave_carry_allocations IN SHARE ROW EXCLUSIVE MODE'))
    request = dict(operation_id=operation_id, reviewed_digest=reviewed_digest,
        source_selection={k:(v.isoformat() if isinstance(v,date) else v) for k,v in source.items()
            if k not in ('current_insurance_years','age_evaluation_dates')},
        insurance=sorted((i,m.isoformat(),str(v) if v is not None else None) for (i,m),v in source['current_insurance_years'].items()),
        age_dates=sorted((m.isoformat(),v.isoformat()) for m,v in source['age_evaluation_dates'].items()),
        full_annual_entitlement=selection['full_annual_entitlement'], payroll_item_id=selection['payroll_item_id'],
        available_from=selection['available_from'].isoformat() if selection['available_from'] else None,
        available_through=selection['available_through'].isoformat() if selection['available_through'] else None,
        reason=selection['reason'].strip(), actor_username=actor.username)
    old = db.get(LeaveAnnualSettlement, operation_id, populate_existing=True)
    if old is not None:
        result = json.loads(old.result_json)
        audits = db.scalars(select(AuditLog).where(AuditLog.action=='leave_annual_settled',
            AuditLog.entity_type=='leave_annual_settlement', AuditLog.entity_id==operation_id)).all()
        if (old.request_json!=canonical(request) or old.request_digest!=digest(request)
                or old.result_digest!=digest(result) or old.source_digest!=reviewed_digest
                or digest(result['plan'])!=reviewed_digest
                or old.employee_id!=source['employee_id'] or old.leave_type_id!=source['leave_type_id']
                or old.source_year!=source['source_year']
                or result['carry_days']!=result['plan']['annual']['carried_days']
                or result['cash_payment_committed'] is not False or result['carry_duplicate_credit']!='0/1'
                or len(audits)!=1 or audits[0].actor!=actor.username or audits[0].old_value!=old.source_digest
                or audits[0].new_value!=old.result_json or audits[0].reason!=request['reason']):
            raise ValueError('Annual recorded operation evidence changed.')
        _verify_plan_rule(db, result['plan']['annual'], old)
        if old.cash_out_id:
            receipt = db.get(LeaveCashOut,old.cash_out_id,populate_existing=True)
            if receipt is None or _verify_cash_receipt(db,receipt)!=result['cash_result']:
                raise ValueError('Annual cash obligation evidence changed.')
        return result
    for cls in (LeaveAnnualSettlement, LeaveCarryAllocation):
        if db.scalar(select(cls.id).where(cls.employee_id==source['employee_id'],
                cls.leave_type_id==source['leave_type_id'],cls.source_year==source['source_year'])):
            raise ValueError('Annual source year already settled or allocated.')
    plan = _review(db, **selection)
    if digest(plan)!=reviewed_digest:
        raise ValueError('Annual sources changed; review again.')
    with db.begin_nested():
        cash = plan['cash']
        cash_result = apply_cash_out(db, operation_id=operation_id, reviewed_digest=plan['cash_reviewed_digest'],
            human_confirmed=True, reason=selection['reason'], employee_id=source['employee_id'],
            leave_type_id=source['leave_type_id'], days=Fraction(plan['annual']['cash_days']),
            payroll_item_id=selection['payroll_item_id'], evaluated_on=source['reviewed_on'], source_year=source['source_year'], **identity) if cash else None
        plan = json.loads(canonical(plan))
        result = dict(plan=plan, cash_result=cash_result, carry_days=plan['annual']['carried_days'],
            cash_payment_committed=False, carry_duplicate_credit='0/1')
        row = LeaveAnnualSettlement(id=operation_id,employee_id=source['employee_id'],leave_type_id=source['leave_type_id'],
            source_year=source['source_year'],cash_out_id=operation_id if cash else None,source_digest=reviewed_digest,
            request_json=canonical(request),request_digest=digest(request),result_json=canonical(result),result_digest=digest(result))
        db.add(row)
        db.add(AuditLog(actor=actor.username,action='leave_annual_settled',entity_type='leave_annual_settlement',
            entity_id=operation_id,old_value=reviewed_digest,new_value=row.result_json,reason=selection['reason'].strip()))
        db.flush()
    return result


def verify_annual_cash_allocation(db, annual, cash):
    """Canonical recorded allocation proof, not live-year re-earning or spending permission."""
    from app.models import LeaveBalanceTransaction
    if (annual.cash_out_id != cash.id or annual.id != cash.id
            or annual.employee_id != cash.employee_id or annual.leave_type_id != cash.leave_type_id
            or json.loads(cash.source_json).get('source_year') != annual.source_year):
        raise ValueError('Annual source-year cash allocation identity changed.')
    row = db.get(LeaveBalanceTransaction,cash.ledger_id,populate_existing=True)
    if row is None:
        raise ValueError('Annual cash allocation ledger missing.')
    source_year = annual.source_year
    result = _verify_cash_receipt(db, cash)
    from app.leave_month_earning import digest as receipt_digest
    from app.models import AuditLog
    recorded = json.loads(annual.result_json)
    request = json.loads(annual.request_json)
    audits = db.scalars(select(AuditLog).where(AuditLog.action == 'leave_annual_settled',
        AuditLog.entity_type == 'leave_annual_settlement', AuditLog.entity_id == annual.id)).all()
    if (annual.result_digest != receipt_digest(recorded)
            or annual.request_digest != receipt_digest(request)
            or annual.source_digest != receipt_digest(recorded['plan'])
            or recorded['cash_result'] != result
            or recorded['plan']['annual']['employee_id'] != annual.employee_id
            or recorded['plan']['annual']['leave_type_id'] != annual.leave_type_id
            or recorded['plan']['cash']['employee_id'] != cash.employee_id
            or recorded['plan']['cash']['leave_type_id'] != cash.leave_type_id
            or request['source_selection']['employee_id'] != annual.employee_id
            or request['source_selection']['leave_type_id'] != annual.leave_type_id
            or request['source_selection']['source_year'] != annual.source_year
            or recorded['carry_days'] != recorded['plan']['annual']['carried_days']
            or recorded['cash_payment_committed'] is not False
            or recorded['carry_duplicate_credit'] != '0/1'
            or recorded['plan']['annual']['source_year'] != source_year
            or recorded['plan']['annual']['cash_days'] != cash.days_canonical
            or recorded['plan']['cash']['source_year'] != source_year
            or len(audits) != 1 or audits[0].new_value != annual.result_json
            or audits[0].old_value != annual.source_digest
            or audits[0].actor != row.created_by or audits[0].reason != row.reason):
        raise ValueError('Annual source-year cash allocation evidence changed.')
    _verify_plan_rule(db, recorded['plan']['annual'], annual)
    return recorded


def _blocking_prior_cash(db, employee_id, leave_type_id, source_year):
    for cash in db.scalars(select(LeaveCashOut).where(LeaveCashOut.employee_id==employee_id,
            LeaveCashOut.leave_type_id==leave_type_id)).all():
        annual = db.get(LeaveAnnualSettlement,cash.id,populate_existing=True)
        if annual is None or annual.source_year >= source_year:
            return cash.id
        verify_annual_cash_allocation(db,annual,cash)
    return None
