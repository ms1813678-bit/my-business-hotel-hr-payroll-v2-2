import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.leave_historical_evidence import HistoricalLeaveFact, prepare_historical_evidence
from app.models import (
    Department,
    Employee,
    LeaveBalanceTransaction,
    LeaveHistoricalEvidencePreparation,
)
from tests.test_department_workbook_store import context  # noqa: F401


@pytest.fixture
def history_context(context):  # noqa: F811
    db, account, _session, auth, _row, _payload = context
    department = Department(name='Synthetic historical department', active=True)
    db.add(department); db.flush()
    employee = Employee(code='SYN-HISTORY', full_name='Synthetic only', department_id=department.id,
        position='Synthetic', hire_date=date(2025, 1, 1), status='active', basic_salary=Decimal(1), daily_hours=Decimal(8))
    db.add(employee); db.flush()
    fact = HistoricalLeaveFact(employee.id, 'A', 'debt_carry', Decimal('-0.12345678901234567890123456789'),
        date(2026, 1, 1), date(2026, 12, 31), 'Synthetic', 'E2', 'Synthetic source review')
    return db, account, auth, fact


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_exact_proposal_replay_and_rollback_without_ledger_effect(history_context):
    db, _account, auth, fact = history_context
    before = count(db, LeaveHistoricalEvidencePreparation)
    ledger = count(db, LeaveBalanceTransaction)
    with db.begin_nested() as tx:
        args = dict(source_bytes=b'synthetic-only-source', source_label='synthetic.xlsx', facts=(fact,), **auth)
        row = prepare_historical_evidence(db, **args)
        assert row.id == prepare_historical_evidence(db, **args).id
        proposal = json.loads(row.proposal_json)
        assert proposal['entries'][0]['days'] == '-0.12345678901234567890123456789'
        assert proposal['requires_human_review'] and not proposal['applied']
        assert count(db, LeaveHistoricalEvidencePreparation) == before + 1
        assert count(db, LeaveBalanceTransaction) == ledger
        tx.rollback()
    assert count(db, LeaveHistoricalEvidencePreparation) == before


@pytest.mark.parametrize('change', [ {'kind': 'opening'}, {'kind': 'carry_grant'}, {'days': Decimal('NaN')},
    {'effective_through': date(2025, 1, 1)}, {'source_cell': ''}, {'employee_id': 2147483647}])
def test_invalid_evidence_has_no_partial_proposal(history_context, change):
    db, _account, auth, fact = history_context
    before = count(db, LeaveHistoricalEvidencePreparation)
    with pytest.raises(ValueError):
        prepare_historical_evidence(db, source_bytes=b'synthetic', source_label='source', facts=(replace(fact, **change),), **auth)
    assert count(db, LeaveHistoricalEvidencePreparation) == before


def test_replay_rechecks_current_hr_and_changed_window_is_separate(history_context):
    db, account, auth, fact = history_context
    args = dict(source_bytes=b'synthetic', source_label='source', facts=(fact,), **auth)
    first = prepare_historical_evidence(db, **args)
    second = prepare_historical_evidence(db, **{**args, 'facts': (replace(fact, effective_through=date(2027, 1, 1)),)})
    assert first.id != second.id
    account.role = 'assistant_manager'; db.flush()
    with pytest.raises(ValueError):
        prepare_historical_evidence(db, **args)


def test_duplicate_coordinates_and_overlaps_refused(history_context):
    db, _account, auth, fact = history_context
    with pytest.raises(ValueError, match='Duplicate'):
        prepare_historical_evidence(db, source_bytes=b'synthetic', source_label='source', facts=(fact, fact), **auth)
    with pytest.raises(ValueError, match='Overlapping'):
        prepare_historical_evidence(db, source_bytes=b'synthetic', source_label='source',
            facts=(fact, replace(fact, source_cell='E3')), **auth)
