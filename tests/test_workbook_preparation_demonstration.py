"""Synthetic preparation-only journey. Proposals are NOT accepted source data.

No real balances, import acceptance, ledger posting, routes or production use.
Opening inputs below are explicit synthetic fixture selections for this demo,
not implicitly authorized by a stored HistoricalEvidencePreparation.
"""
import json
from datetime import date
from decimal import Decimal
from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import func, select

from app.department_workbook import ENTRY_SHEET, FIRST_EMPLOYEE_ROW, build_department_workbook
from app.department_workbook_projection import project_earned_workbook
from app.department_workbook_source import read_workbook_source
from app.department_workbook_store import persist_export, prepare_return
from app.department_workbook_tiers import resolve_workbook_tiers
from app.leave_historical_evidence import prepare_historical_evidence
from app.leave_policy_preview import LeavePolicy
from app.models import Employee, EmployeeServicePeriod, LeaveBalanceTransaction, LeaveRegisterEntry
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401


def test_synthetic_review_preparation_to_planning_export_and_return(history_context):  # noqa: F811
    db, _account, auth, fact = history_context
    before_ledger = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    before_register = db.scalar(select(func.count()).select_from(LeaveRegisterEntry))
    proposal = prepare_historical_evidence(db, source_bytes=b'synthetic-not-an-upload',
        source_label='synthetic-example.csv', facts=(fact,), **auth)
    assert json.loads(proposal.proposal_json)['requires_human_review']
    assert not json.loads(proposal.proposal_json)['applied']
    employee = db.get(Employee, fact.employee_id)
    employee.birth_date = date(1990, 1, 1)
    employee.prior_insurance_years = Decimal(0)
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=date(2025, 1, 1)))
    db.flush()
    policy = LeavePolicy(True, True, True, False, 'calendar', 'prorated', False, (90, 180))
    facts = read_workbook_source(db, department_id=employee.department_id,
        employee_ids=(employee.id,), window_start=date(2026, 1, 1), month_start=date(2026, 1, 1),
        policy_digest='a'*64, expected_epoch=auth['expected_epoch'])
    tiers = resolve_workbook_tiers(facts=facts, policy=policy,
        age_evaluation_dates={date(2026, 1, 1): date(2026, 1, 1)},
        current_insurance_years={(employee.id, date(2026, 1, 1)): Decimal(1)})
    assert not tiers.unresolved
    # Explicit synthetic fixture values, NOT retrieved/accepted from proposal.
    opening = {(employee.id, code): Decimal(0) for code in ('A', 'H', 'W')}
    opening[employee.id, 'A'] = fact.days
    projection = project_earned_workbook(facts=facts, policy=policy, opening=opening,
        annual_tiers=tiers.tiers, planning_target_month=True)
    assert projection.unresolved_target_days == ((employee.id, 31),)
    assert not projection.establishes_spendable_credit
    payload, manifest = build_department_workbook(department='Synthetic demonstration',
        month_start=date(2026, 1, 1), employees=projection.employees)
    exported = persist_export(db, manifest=manifest, payload=payload,
        source_digest=projection.source_digest, **auth)
    workbook = load_workbook(BytesIO(payload))
    workbook[ENTRY_SHEET].cell(FIRST_EMPLOYEE_ROW, 3).value = 'W'
    returned = BytesIO(); workbook.save(returned); workbook.close()
    prepared = prepare_return(db, export_id=exported.id, payload=returned.getvalue(),
        current_source_digest=projection.source_digest, **auth)
    evidence = json.loads(prepared.evidence_json)
    assert evidence[0]['code'] == 'W'
    assert evidence[1]['code'] is None
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == before_ledger
    assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry)) == before_register
    assert not json.loads(proposal.proposal_json)['applied']
