"""Closed calendar-year source verification only, never carry or cash posting."""
from datetime import date, datetime
from fractions import Fraction
import json

from sqlalchemy import select

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_carry_source import review_carry_source
from app.leave_month_earning import _locks, _derive, _verify_receipt, digest
from app.leave_opening_receipt import verified_manual_opening
from app.leave_opening_source import read_confirmed_opening_source
from app.leave_year_coverage import validate_earning_month_coverage
from app.department_workbook_source import read_workbook_source
from app.department_workbook_tiers import resolve_workbook_tiers
from app.models import Employee
from app.models import CAIRO_TZ, EmployeeServicePeriod, LeaveMonthEarning


@bounded_workbook_operation
def review_completed_year_source(db, *, employee_id, leave_type_id, source_year,
        selection_id, reviewed_on, current_insurance_years, age_evaluation_dates, **identity):
    """Prove explicit year attribution, earning coverage and fresh final sources.

    A future reviewed date cannot close the current calendar year early. Existing
    monthly receipts and their audits/ledger are verified, then the live guarded
    calculation checks register/service/policy and the actual selected baseline.
    Held annual remains a separate exact amount, not spendable ledger credit.
    """
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean completed-year review transaction required.')
    if (type(source_year) is not int or not 1 <= source_year <= 9998
            or type(reviewed_on) is not date or reviewed_on < date(source_year + 1, 1, 1)
            or reviewed_on > datetime.now(CAIRO_TZ).date()):
        raise ValueError('Actual completed calendar year and current review date required.')
    # Lock the richest existing source writer first. Never upgrade employees or
    # ledger SHARE locks into its audit-first order after another writer starts.
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
    # Existing monthly source supports an explicit baseline in the same source
    # year. Do not pretend older snapshots establish cross-year accrual coverage.
    if baseline.year != source_year:
        raise ValueError('Source-year baseline needs a separately reviewed year transition.')
    periods = db.scalars(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id == employee_id)).all()
    receipts = db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id == employee_id,
        LeaveMonthEarning.month_start >= date(source_year, 1, 1),
        LeaveMonthEarning.month_start <= date(source_year, 12, 1)).order_by(LeaveMonthEarning.month_start)).all()
    coverage = validate_earning_month_coverage(source_year=source_year, baseline=baseline,
        service_periods=tuple((p.start_date, p.end_date) for p in periods), reviewed_on=reviewed_on,
        receipt_months=tuple(r.month_start for r in receipts))
    if not coverage:
        raise ValueError('No earned source-month coverage; separate zero-source review required.')
    from app.leave_opening_rebind import lineage_ids
    allowed = lineage_ids(db, selection_id)
    for receipt in receipts:
        if receipt.selection_id not in allowed:
            raise ValueError('Mixed monthly source selections refuse year close.')
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
    result = review_carry_source(db, employee_id=employee_id, leave_type_id=leave_type_id,
        source_year=source_year, reviewed_on=reviewed_on, **identity)
    payload = dict(source=result['source'], selection_id=selection_id, opening_digest=opening_digest,
        opening_apply=opening['apply_sha256'], covered_months=[m.isoformat() for m in coverage],
        verified_months=[(r.id, r.source_digest, r.result_digest) for r in receipts],
        live_final_source=digest(live), current_tiers=sorted((m.isoformat(), t) for (i, m), t in tiers.tiers.items()), reviewed_on=reviewed_on.isoformat())
    return dict(source=payload, source_digest=digest(payload), amount=result['amount'],
        held_annual=sum((Fraction(v) for _, v in result['source']['held_annual']), Fraction()),
        source_verified=True, posting_allowed=False, unresolved=())
