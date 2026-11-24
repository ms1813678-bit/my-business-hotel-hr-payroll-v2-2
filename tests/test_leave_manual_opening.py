from datetime import date
from decimal import Decimal

import pytest

from app.leave_manual_opening import prepare_manual_opening, review_manual_opening
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401


def test_manual_exact_zero_and_positive_proposals_not_applied(history_context):  # noqa: F811
    db, _account, auth, fact = history_context
    for code, amount in [('A', Decimal('1.12345678901234567890123456789')), ('H', Decimal(0))]:
        row = prepare_manual_opening(db, employee_id=fact.employee_id, code=code, days=amount,
            effective_start=date(2026, 5, 1), reason='Synthetic manual entry', **auth)
        review = review_manual_opening(row)
        assert review['days'] == format(amount, 'f')
        assert review['effective_start'] == '2026-05-01'
        assert review['requires_human_review'] and not review['applied']


def test_negative_manual_opening_does_not_become_carry(history_context):  # noqa: F811
    db, _account, auth, fact = history_context
    with pytest.raises(ValueError, match='debt carry is separate'):
        prepare_manual_opening(db, employee_id=fact.employee_id, code='A', days=Decimal(-1),
            effective_start=date(2026, 1, 1), reason='Synthetic', **auth)


def test_manual_review_to_source_projection_is_preview_only(history_context):  # noqa: F811
    from app.department_workbook_source import read_workbook_source
    from app.leave_manual_opening import manual_opening_projection_preview
    from app.leave_policy_preview import LeavePolicy
    from app.models import Employee, EmployeeServicePeriod
    db, _account, auth, fact = history_context
    employee = db.get(Employee, fact.employee_id)
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=date(2025, 1, 1))); db.flush()
    reviews = []
    for code, days in [('A', '8.25'), ('H', '1'), ('W', '2')]:
        row = prepare_manual_opening(db, employee_id=employee.id, code=code, days=Decimal(days),
            effective_start=date(2026, 5, 1), reason='Synthetic reviewed input', **auth)
        reviews.append(review_manual_opening(row))
    facts = read_workbook_source(db, department_id=employee.department_id, employee_ids=(employee.id,),
        window_start=date(2026, 1, 1), month_start=date(2026, 5, 1), policy_digest='a'*64,
        expected_epoch=auth['expected_epoch'])
    policy = LeavePolicy(False, False, True, False, 'calendar', 'prorated', False, (90, 180))
    result = manual_opening_projection_preview(db=db, reviews=reviews, facts=facts, policy=policy,
        annual_tiers={(employee.id, date(2026, m, 1)): 15 for m in range(1, 6)}, planning_target_month=True)
    assert result.employees[0].annual.carry == Decimal('8.25')
    assert result.employees[0].annual.entitled_through_month_end == Decimal('1.25')
    assert result.employees[0].annual.used_before_month == 0
    assert not result.establishes_spendable_credit
    assert all(not review['applied'] for review in reviews)


def test_midmonth_workbook_preview_clips_only_usage_not_attendance(history_context):  # noqa: F811
    from app.department_workbook_source import read_workbook_source
    from app.leave_manual_opening import manual_opening_projection_preview
    from app.leave_policy_preview import LeavePolicy
    from app.models import Employee, EmployeeServicePeriod
    db, _account, auth, fact = history_context
    employee = db.get(Employee, fact.employee_id)
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=date(2025, 1, 1))); db.flush()
    reviews = [review_manual_opening(prepare_manual_opening(db, employee_id=employee.id, code=code,
        days=Decimal(1), effective_start=date(2026, 5, 17), reason='Synthetic', **auth)) for code in ('A', 'H', 'W')]
    facts = read_workbook_source(db, department_id=employee.department_id, employee_ids=(employee.id,),
        window_start=date(2026, 1, 1), month_start=date(2026, 5, 1), policy_digest='a'*64, expected_epoch=auth['expected_epoch'])
    result = manual_opening_projection_preview(db=db, reviews=reviews, facts=facts,
        policy=LeavePolicy(False, False, True, False, 'calendar', 'prorated', False, (90, 180)),
        annual_tiers={(employee.id, date(2026, m, 1)): 15 for m in range(1, 6)}, planning_target_month=True)
    from io import BytesIO

    from openpyxl import load_workbook

    from app.department_workbook import BALANCE_SHEET, ENTRY_SHEET, inspect_department_return
    from app.leave_manual_opening import build_manual_opening_preview_workbook
    payload, manifest = build_manual_opening_preview_workbook(projection=result, department='Synthetic', month_start=date(2026,5,1))
    workbook = load_workbook(BytesIO(payload))
    assert 'S7:AG7' in workbook[BALANCE_SHEET]['E7'].value
    assert not workbook[ENTRY_SHEET]['C7'].protection.locked
    assert '2026-05-17' in workbook[BALANCE_SHEET]['A3'].value
    workbook.close()
    assert not inspect_department_return(payload, manifest).issues
    from app.department_workbook import build_department_workbook
    plain, _manifest = build_department_workbook(department='Synthetic', month_start=date(2026,5,1), employees=result.employees)
    plain_wb = load_workbook(BytesIO(plain))
    assert 'S7:AG7' in plain_wb[BALANCE_SHEET]['E7'].value
    plain_wb.close()
    from dataclasses import replace
    with pytest.raises(ValueError, match='changed'):
        build_manual_opening_preview_workbook(projection=replace(result, opening_effective_start=date(2026,5,1)), department='Synthetic', month_start=date(2026,5,1))
    with pytest.raises(ValueError, match='binding'):
        manual_opening_projection_preview(db=db, reviews=[{**r, 'days':'2'} for r in reviews], facts=facts,
            policy=LeavePolicy(False, False, True, False, 'calendar', 'prorated', False, (90,180)),
            annual_tiers={(employee.id,date(2026,m,1)):15 for m in range(1,6)})


def test_group_atomic_on_late_category_invalid(history_context):  # noqa: F811
    from sqlalchemy import func, select

    from app.leave_manual_opening import prepare_manual_opening_group
    from app.models import AuditLog, LeaveHistoricalEvidencePreparation
    db, _account, auth, fact = history_context
    before = db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation))
    audit_before = db.scalar(select(func.count()).select_from(AuditLog))
    with pytest.raises(ValueError):
        prepare_manual_opening_group(db, employee_id=fact.employee_id,
            balances={'A': Decimal(2), 'W': Decimal(1), 'H': Decimal(-1)},
            effective_start=date(2026, 1, 1), reason='Synthetic group', **auth)
    assert db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation)) == before
    assert db.scalar(select(func.count()).select_from(AuditLog)) == audit_before


def test_equivalent_amounts_dedupe_and_negative_zero_canonical(history_context):  # noqa: F811
    db, _account, auth, fact = history_context
    args = dict(employee_id=fact.employee_id, code='A', effective_start=date(2026,1,1), reason='Synthetic', **auth)
    one = prepare_manual_opening(db, days=Decimal('1.00'), **args)
    assert one.id == prepare_manual_opening(db, days=Decimal(1), **args).id
    zero = prepare_manual_opening(db, days=Decimal('-0.00'), **args)
    assert review_manual_opening(zero)['days'] == '0'
    assert zero.id == prepare_manual_opening(db, days=Decimal('0.0'), **args).id


def test_crafted_negative_review_and_large_workbook_projection_refuse(history_context):  # noqa: F811
    import json

    from app.department_workbook_source import WorkbookSourceFacts
    from app.leave_manual_opening import manual_opening_projection_inputs
    db, _account, auth, fact = history_context
    with pytest.raises(ValueError):
        prepare_manual_opening(db, employee_id=fact.employee_id, code='A', days=Decimal('1E+100'),
            effective_start=date(2026,1,1), reason='Synthetic', **auth)
    source = {'window_start':'2026-01-01','month_start':'2026-05-01',
        'employees':[{'id':fact.employee_id}], 'service_periods':[{'employee_id':fact.employee_id,'start_date':'2025-01-01'}]}
    facts = WorkbookSourceFacts(json.dumps(source), 'a'*64, (), ())
    with pytest.raises(ValueError, match='negative'):
        manual_opening_projection_inputs(facts=facts, reviews=[{'employee_id':fact.employee_id,
            'code':'A','days':'-3','effective_start':'2026-01-01'}])


@pytest.mark.parametrize('corruption', ['missing_days','bad_days','bad_employee','bad_date','non_object','non_entry','spaced_days','underscored_days','exponent_days'])
def test_malformed_selfhashed_manual_source_refuses_cleanly(history_context,corruption):
    import json
    from hashlib import sha256
    db,_account,auth,fact=history_context
    row=prepare_manual_opening(db,employee_id=fact.employee_id,code='A',days=Decimal(1),
        effective_start=date(2026,5,1),reason='Synthetic',**auth)
    value=json.loads(row.proposal_json)
    if corruption=='missing_days': del value['entries'][0]['days']
    elif corruption=='bad_days': value['entries'][0]['days']='not-a-number'
    elif corruption=='bad_employee': value['entries'][0]['employee_id']=True
    elif corruption=='bad_date': value['entries'][0]['effective_from']='invalid'
    elif corruption=='spaced_days': value['entries'][0]['days']=' 8 '
    elif corruption=='underscored_days': value['entries'][0]['days']='1_0'
    elif corruption=='exponent_days': value['entries'][0]['days']='1E+999999'
    elif corruption=='non_object': value=[]
    else: value['entries']=[None]
    row.proposal_json=json.dumps(value);row.proposal_sha256=sha256(row.proposal_json.encode()).hexdigest()
    with pytest.raises(ValueError): review_manual_opening(row)
