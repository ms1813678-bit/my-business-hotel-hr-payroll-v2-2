"""Read-only net source for a completed later calendar year (year 2 and after).

Composition only, no new policy and no posting. For source year Y after the
opening baseline year, the net of Y is:
  * exact ledger rows tagged Y, each proven by review_carry_source (monthly
    receipts, cash/zero/expiry provenance), plus
  * the register days of Y's aggregate usage debit that HR explicitly attributed
    to an earlier-year grant (those days consumed the carried grant, not Y).
Expiry and zero debits carry their grant's own year tag, so they never enter Y.
Every earlier year needs a verified closing checkpoint. No netting across years,
no FIFO, no automatic expiry, no ledger write. Caller owns the transaction.
"""
from datetime import date, datetime
from fractions import Fraction
import json

from sqlalchemy import select

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_source import read_workbook_source
from app.department_workbook_tiers import resolve_workbook_tiers
from app.leave_carry_consumption import _grant, _usage, _verify_recorded
from app.leave_carry_source import review_carry_source
from app.leave_exact_amount import as_fraction, canonical_amount, exact_sum, ledger_amount
from app.leave_month_earning import _derive, _locks, _verify_receipt, digest
from app.leave_opening_receipt import verified_manual_opening
from app.leave_opening_rebind import lineage_ids
from app.leave_opening_source import read_confirmed_opening_source
from app.leave_year_coverage import validate_earning_month_coverage
from app.leave_year_transition import verify_transition_for_month
from app.models import (CAIRO_TZ, Employee, EmployeeServicePeriod, LeaveAnnualSettlement, LeaveBalanceTransaction,
                        LeaveCarryAllocation, LeaveCarryConsumption, LeaveMonthEarning)

IDENTITY_KEYS = ('actor_id', 'session_id', 'session_version', 'expected_epoch')


def _grant_year(db, row):
    model = LeaveCarryAllocation if row.grant_kind == 'carry' else LeaveAnnualSettlement
    grant = db.get(model, row.grant_id, populate_existing=True)
    if (grant is None or grant.employee_id != row.employee_id or grant.leave_type_id != row.leave_type_id):
        raise ValueError('Consumed grant evidence missing.')
    return grant.source_year


@bounded_workbook_operation
def review_year2_net_source(db, *, employee_id, leave_type_id, source_year, selection_id, reviewed_on,
                            current_insurance_years, age_evaluation_dates, **identity):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean year-net transaction required.')
    if (type(employee_id) is not int or type(leave_type_id) is not int or type(source_year) is not int
            or not 2 <= source_year <= 9998 or type(reviewed_on) is not date
            or reviewed_on < date(source_year + 1, 1, 1) or reviewed_on > datetime.now(CAIRO_TZ).date()):
        raise ValueError('Actual completed later calendar year and current review date required.')
    if set(identity) != set(IDENTITY_KEYS):
        raise ValueError('Exact current session identity required.')
    _locks(db, identity)
    reviews, policy, opening_digest = read_confirmed_opening_source(db, selection_id=selection_id, **identity)
    opening = verified_manual_opening(db, selection_id=selection_id, **identity)
    if opening['employee_id'] != employee_id or {r['employee_id'] for r in reviews} != {employee_id}:
        raise ValueError('Completed-year selection belongs to another employee.')
    if policy.annual_year_basis != 'calendar':
        raise ValueError('Calendar year source requires the explicit calendar policy.')
    baselines = {date.fromisoformat(r['effective_start']) for r in reviews}
    if len(baselines) != 1:
        raise ValueError('One explicit opening baseline required.')
    baseline = baselines.pop()
    if baseline.year >= source_year:
        raise ValueError('Opening-year source uses the completed-year source, not the later-year net.')
    transitions = verify_transition_for_month(db, employee_id=employee_id, selection_id=selection_id,
        month_start=date(source_year, 1, 1), identity=identity)
    periods = db.scalars(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id == employee_id)).all()
    receipts = db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id == employee_id,
        LeaveMonthEarning.month_start >= date(source_year, 1, 1),
        LeaveMonthEarning.month_start <= date(source_year, 12, 1)).order_by(LeaveMonthEarning.month_start)).all()
    coverage = validate_earning_month_coverage(source_year=source_year, baseline=baseline,
        service_periods=tuple((p.start_date, p.end_date) for p in periods), reviewed_on=reviewed_on,
        receipt_months=tuple(r.month_start for r in receipts))
    if not coverage:
        raise ValueError('No earned source-month coverage; separate zero-source review required.')
    allowed = lineage_ids(db, selection_id)
    for receipt in receipts:
        if receipt.selection_id not in allowed:
            raise ValueError('Mixed monthly source selections refuse year net.')
        _verify_receipt(db, receipt)
    live, _ = _derive(db, selection_id=selection_id, month_start=coverage[-1], evaluated_on=reviewed_on,
        current_insurance_years=current_insurance_years, age_evaluation_dates=age_evaluation_dates, **identity)
    employee = db.get(Employee, employee_id, populate_existing=True)
    facts = read_workbook_source(db, department_id=employee.department_id, employee_ids=(employee_id,),
        window_start=date(source_year, 1, 1), month_start=coverage[-1], policy_digest=opening_digest,
        expected_epoch=identity['expected_epoch'])
    tiers = resolve_workbook_tiers(facts=facts, policy=policy, current_insurance_years=current_insurance_years,
        age_evaluation_dates=age_evaluation_dates)
    if tiers.unresolved:
        raise ValueError('Resolve explicit year tier evidence before completion.')
    for receipt in receipts:
        original = json.loads(receipt.source_json)
        if (original['tier'] != tiers.tiers[employee_id, receipt.month_start]
                or original['opening'] != opening['apply_sha256'] or original['baseline'] != baseline.isoformat()):
            raise ValueError('Live earned month tier or baseline changed; separate correction required.')
    own = review_carry_source(db, employee_id=employee_id, leave_type_id=leave_type_id,
        source_year=source_year, reviewed_on=reviewed_on, **identity)
    tagged = {row.id for row in db.scalars(select(LeaveBalanceTransaction).where(
        LeaveBalanceTransaction.employee_id == employee_id, LeaveBalanceTransaction.leave_type_id == leave_type_id,
        LeaveBalanceTransaction.earning_year == source_year)).all()}
    attributed, bindings = Fraction(), []
    history = db.scalars(select(LeaveCarryConsumption).where(LeaveCarryConsumption.employee_id == employee_id,
        LeaveCarryConsumption.leave_type_id == leave_type_id).order_by(LeaveCarryConsumption.id).limit(10001)).all()
    if len(history) > 10000:
        raise ValueError('Carry attribution history exceeds bounded review scope.')
    grant_used, day_used, row_used = {}, {}, {}
    for row in history:
        _request, saved = _verify_recorded(db, row)
        plan = saved['plan']
        usage = _usage(db, monthly_receipt_id=plan['monthly_receipt_id'], usage_ledger_id=row.usage_ledger_id,
            employee_id=employee_id, leave_type_id=leave_type_id, work_date=row.work_date, identity=identity)
        grant = _grant(db, grant_kind=row.grant_kind, grant_id=row.grant_id, employee_id=employee_id,
            leave_type_id=leave_type_id, work_date=row.work_date, identity=identity)
        if usage != plan['usage'] or grant != plan['grant']:
            raise ValueError('Carry attribution usage or grant evidence changed.')
        grant_year = _grant_year(db, row)
        qty = Fraction(row.quantity_canonical)
        key = (row.grant_kind, row.grant_id)
        grant_used[key] = grant_used.get(key, Fraction()) + qty
        day_used[row.work_date] = day_used.get(row.work_date, Fraction()) + qty
        row_used[row.usage_ledger_id] = row_used.get(row.usage_ledger_id, Fraction()) + qty
        if (grant_used[key] > Fraction(grant['ceiling']) or day_used[row.work_date] > 1
                or row_used[row.usage_ledger_id] > Fraction(usage['amount'])):
            raise ValueError('Carry attribution exceeds grant, day or usage cap.')
        if row.usage_ledger_id in tagged:
            if grant_year >= source_year:
                raise ValueError('Usage attributed to a same-year or later grant cannot net a source year.')
            attributed += qty
            bindings.append([row.id, row.source_digest, row.result_digest, grant_year])
    all_rows = db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id == employee_id,
        LeaveBalanceTransaction.leave_type_id == leave_type_id).order_by(LeaveBalanceTransaction.id)).all()
    own_amount = as_fraction(own['amount'])
    net = own_amount + attributed
    held = sum((Fraction(v) for _, v in own['source']['held_annual']), Fraction())
    payload = dict(semantic='read_only_source_not_available_balance', employee_id=employee_id, leave_type_id=leave_type_id, source_year=source_year,
        held_annual_not_in_net=canonical_amount(held),
        selection_id=selection_id, own_source=own['source_digest'], own_rows_amount=canonical_amount(own_amount),
        attributed_to_prior_grants=canonical_amount(attributed), net_amount=canonical_amount(net),
        attributions=bindings, transitions=transitions, covered_months=[m.isoformat() for m in coverage],
        verified_months=[(r.id, r.source_digest, r.result_digest) for r in receipts], live_final_source=digest(live),
        all_time_balance=canonical_amount(exact_sum(ledger_amount(r) for r in all_rows)),
        reviewed_on=reviewed_on.isoformat())
    return dict(source=payload, source_digest=digest(payload), amount=net, own_rows_amount=own_amount,
        attributed=attributed, source_verified=True, posting_allowed=False, unresolved=())
