from datetime import date
from decimal import Decimal

from app.department_workbook_source import read_workbook_source
from app.department_workbook_tiers import resolve_workbook_tiers
from app.leave_policy_preview import LeavePolicy
from tests.test_department_workbook_source import source_context  # noqa: F401


def test_real_employee_fields_and_unknown_insurance_are_not_tenure(source_context):  # noqa: F811
    db, employee, args = source_context
    employee.birth_date = date(1990, 1, 1)
    employee.prior_insurance_years = Decimal(3)
    db.flush()
    args = {**args, 'month_start': date(2026, 1, 1)}
    facts = read_workbook_source(db, **args)
    policy = LeavePolicy(True, True, True, False, 'calendar', 'prorated', False, (90, 180))
    inputs = {'facts': facts, 'policy': policy, 'age_evaluation_dates': {date(2026, 1, 1): date(2026, 1, 1)}}
    unknown = resolve_workbook_tiers(**inputs, current_insurance_years={(employee.id, date(2026, 1, 1)): None})
    assert not unknown.tiers and unknown.unresolved[0][2] == 'insured_years'
    resolved = resolve_workbook_tiers(**inputs, current_insurance_years={(employee.id, date(2026, 1, 1)): Decimal(7)})
    assert resolved.tiers[employee.id, date(2026, 1, 1)] == 30
    assert not resolved.unresolved


def test_anniversary_month_not_day_and_source_digest_includes_birth(source_context):  # noqa: F811
    db, employee, args = source_context
    args = {**args, 'month_start': date(2026, 2, 1)}
    employee.hire_date = date(2025, 2, 20)
    employee.birth_date = date(1990, 1, 1)
    db.flush()
    before = read_workbook_source(db, **args)
    policy = LeavePolicy(False, False, True, False, 'calendar', 'prorated', False, (90, 180))
    result = resolve_workbook_tiers(facts=before, policy=policy,
        age_evaluation_dates={date(2026, m, 1): date(2026, m, 1) for m in (1, 2)},
        current_insurance_years={(employee.id, date(2026, m, 1)): None for m in (1, 2)})
    assert result.tiers[employee.id, date(2026, 1, 1)] == 15
    assert result.tiers[employee.id, date(2026, 2, 1)] == 21
    employee.birth_date = date(1970, 1, 1); db.flush()
    assert before.digest != read_workbook_source(db, **args).digest
