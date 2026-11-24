from datetime import date
from decimal import Decimal

import pytest

from app.department_workbook_projection import project_earned_workbook
from app.department_workbook_source import read_workbook_source
from app.leave_policy_preview import LeavePolicy
from app.models import EmployeeServicePeriod, LeaveRegisterEntry
from tests.test_department_workbook_source import source_context  # noqa: F401


@pytest.fixture
def projection_context(source_context):  # noqa: F811
    db, employee, args = source_context
    # Target January: actual source facts, deliberately synthetic supplied policy.
    args = {**args, 'month_start': date(2026, 1, 1)}
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=date(2026, 1, 1)))
    for n in range(1, 32):
        db.add(LeaveRegisterEntry(employee_id=employee.id, work_date=date(2026, 1, n),
            code='W' if n == 2 else '', source='synthetic', recorded_by='synthetic', revision=1))
    db.flush()
    facts = read_workbook_source(db, **args)
    policy = LeavePolicy(True, True, True, False, 'calendar', 'prorated', False, (90, 180))
    opening = {(employee.id, c): Decimal(-1) for c in ('A', 'H', 'W')}
    tiers = {(employee.id, date(2026, 1, 1)): 15}
    return facts, policy, opening, tiers


def test_real_source_projection_is_earned_only_exact_and_not_current_ledger_sum(projection_context):
    facts, policy, opening, tiers = projection_context
    result = project_earned_workbook(facts=facts, policy=policy, opening=opening, annual_tiers=tiers)
    employee = result.employees[0]
    assert result.earned_only and not result.establishes_spendable_credit
    assert employee.annual.entitled_through_month_end == Decimal('1.25')
    assert employee.weekly.entitled_through_month_end == Decimal('4.4285714285714285714285714285714285714285714285714')
    assert employee.weekly.used_before_month == 0  # Target-month usage stays live in workbook.
    assert employee.weekly.carry == -1
    assert len(employee.employed_dates) == 31
    modified = {key: value + 1 for key, value in opening.items()}
    assert result.source_digest != project_earned_workbook(facts=facts, policy=policy, opening=modified, annual_tiers=tiers).source_digest


@pytest.mark.parametrize('missing', ['opening', 'tier'])
def test_missing_projection_inputs_never_inferred_zero(projection_context, missing):
    facts, policy, opening, tiers = projection_context
    if missing == 'opening': opening = {}
    else: tiers = {}
    with pytest.raises(ValueError):
        project_earned_workbook(facts=facts, policy=policy, opening=opening, annual_tiers=tiers)


def test_cumulative_projection_separates_prior_usage_from_target_month(source_context):  # noqa: F811
    db, employee, args = source_context
    args = {**args, 'month_start': date(2026, 2, 1)}
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=date(2026, 1, 31)))
    from datetime import timedelta
    for offset in range(29):
        day = date(2026, 1, 31) + timedelta(days=offset)
        db.add(LeaveRegisterEntry(employee_id=employee.id, work_date=day,
            code='A' if offset == 0 else '', source='synthetic', recorded_by='synthetic', revision=1))
    db.flush()
    facts = read_workbook_source(db, **args)
    policy = LeavePolicy(True, True, True, False, 'calendar', 'prorated', False, (90, 180))
    result = project_earned_workbook(facts=facts, policy=policy,
        opening={(employee.id, c): Decimal(0) for c in ('A', 'H', 'W')},
        annual_tiers={(employee.id, date(2026, m, 1)): 15 for m in (1, 2)})
    assert result.employees[0].annual.used_before_month == 1
    assert len(result.employees[0].employed_dates) == 28
    assert result.employees[0].annual.entitled_through_month_end > Decimal('1.25')


def test_missing_day_stays_unresolved_not_present(projection_context):
    import json
    from dataclasses import replace
    facts, policy, opening, tiers = projection_context
    source = json.loads(facts.canonical_json)
    source['register'][0]['code'] = None
    changed = replace(facts, canonical_json=json.dumps(source))
    with pytest.raises(ValueError, match='Unknown'):
        project_earned_workbook(facts=changed, policy=policy, opening=opening, annual_tiers=tiers)


def test_explicit_target_planning_preserves_unknown_not_presence(projection_context):
    import json
    from dataclasses import replace
    facts, policy, opening, tiers = projection_context
    source = json.loads(facts.canonical_json)
    source['register'] = []
    changed = replace(facts, canonical_json=json.dumps(source))
    result = project_earned_workbook(facts=changed, policy=policy, opening=opening,
                                    annual_tiers=tiers, planning_target_month=True)
    assert result.unresolved_target_days == ((result.employees[0].employee_id, 31),)
    assert result.employees[0].annual.entitled_through_month_end == Decimal('1.25')
    assert not result.establishes_spendable_credit
    assert json.loads(changed.canonical_json)['register'] == []
    source['register'] = [{'employee_id': result.employees[0].employee_id, 'work_date': '2026-01-02', 'code': 'U'}]
    reduced = project_earned_workbook(facts=replace(changed, canonical_json=json.dumps(source)),
        policy=policy, opening=opening, annual_tiers=tiers, planning_target_month=True)
    assert reduced.employees[0].annual.entitled_through_month_end < Decimal('1.25')
    assert reduced.unresolved_target_days[0][1] == 30


def test_midmonth_opening_clips_accrual_not_calendar_denominator(projection_context):
    facts, policy, opening, tiers = projection_context
    result = project_earned_workbook(facts=facts, policy=policy, opening=opening,
        annual_tiers=tiers, opening_effective_start=date(2026, 1, 17))
    row = result.employees[0]
    assert row.annual.entitled_through_month_end == Decimal('0.60483870967741935483870967741935483870967741935484')
    assert len(row.employed_dates) == 31  # Opening basis is NOT employment start.
    assert row.window_start == date(2026, 1, 1)
    assert result.opening_effective_start == date(2026, 1, 17)
    assert row.annual.carry == Decimal(-1)
