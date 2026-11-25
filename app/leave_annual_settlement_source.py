"""Source-bound annual year-end review, no cash payment or ledger mutation.

Full entitlement is verified against one unambiguous earned-year tier. A year
with changing tiers refuses here until an explicit entitlement-basis workflow
exists. No December-tier, average-tier or half-unused-balance policy is invented.
"""
from fractions import Fraction

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_annual_settlement import annual_settlement_split
from app.leave_completed_year_source import review_completed_year_source
from app.leave_exact_amount import canonical_amount, exact_sum
from app.leave_month_earning import digest
from app.models import Employee


@bounded_workbook_operation
def review_annual_settlement(db, *, source_selection, full_annual_entitlement, other_half_action):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean annual settlement review required.')
    source = review_completed_year_source(db, **source_selection)
    if source['source']['source']['leave_type_code'] != 'annual':
        raise ValueError('Annual settlement requires the actual annual leave type.')
    covered = set(source['source']['covered_months'])
    earned_tiers = {tier for month, tier in source['source']['current_tiers'] if month in covered}
    if len(earned_tiers) != 1 or type(full_annual_entitlement) is not int or earned_tiers != {full_annual_entitlement}:
        raise ValueError('Full annual entitlement must match one unambiguous verified earned-year tier.')
    employee = db.get(Employee, source_selection['employee_id'], populate_existing=True)
    total = exact_sum((source['amount'], source['held_annual']))
    released = source['held_annual'] == 0
    split = annual_settlement_split(full_annual_entitlement=full_annual_entitlement,
        remaining=total, hire_date=employee.hire_date, source_year=source_selection['source_year'],
        other_half_action=other_half_action, annual_release_proven=released)
    # The owner's latest examples explicitly retain the full entitlement / 2
    # basis, cap it by positive unused days, then carry the unused amount left.
    payload = dict(employee_id=employee.id, leave_type_id=source_selection['leave_type_id'],
        source_year=source_selection['source_year'], source_digest=source['source_digest'],
        source=source['source'], full_annual_entitlement=full_annual_entitlement,
        remaining=canonical_amount(total), released_remaining=canonical_amount(source['amount']),
        held_remaining=canonical_amount(source['held_annual']), other_half_action=other_half_action,
        cash_days=canonical_amount(split.cash_days), carried_days=canonical_amount(split.carried_days),
        cleared_days=canonical_amount(split.cleared_days), held_days=canonical_amount(split.held_days),
        late_year_preservation=split.late_year_preservation, ledger_delta='0/1',
        cash_payment_committed=False, operational_spending_allowed=False)
    if Fraction(payload['cash_days']) + Fraction(payload['carried_days']) + Fraction(payload['cleared_days']) != total:
        raise ValueError('Annual settlement does not conserve the verified unused source.')
    return dict(plan=payload, reviewed_digest=digest(payload), posting_allowed=False)
