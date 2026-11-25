from datetime import date
from fractions import Fraction
import pytest
from sqlalchemy import select, func, text
from app.leave_carry_allocation_store import review_carry_allocation, record_carry_allocation
from app.models import LeaveCarryAllocation, LeaveBalanceTransaction, LeaveRegisterEntry
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa


def call(args):
    return dict(source_selection=args, available_from=date(2026, 1, 1),
        available_through=date(2026, 4, 30), reason='Synthetic explicit carry window')


def test_review_record_replay_preserves_exact_balance_without_duplicate_credit(prior_year):
    db, employee, kinds, args = prior_year
    before = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    selection = call(args); review = review_carry_allocation(db, **selection)
    assert review['plan']['carried_amount'] == '37/7'
    assert not review['posting_allowed']
    confirm = dict(operation_id='e'*64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **selection)
    first = record_carry_allocation(db, **confirm)
    assert first == record_carry_allocation(db, **confirm)
    assert first['ledger_delta'] == '0/1' and not first['operational_spending_allowed']
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == before
    assert db.scalar(select(func.count()).select_from(LeaveCarryAllocation)) == 1
    with pytest.raises(ValueError, match='already allocated'):
        record_carry_allocation(db, **{**confirm, 'operation_id': 'f'*64})


def test_stale_register_or_unconfirmed_refuses_no_receipt(prior_year):
    db, employee, kinds, args = prior_year
    selection = call(args); review = review_carry_allocation(db, **selection)
    confirm = dict(operation_id='e'*64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **selection)
    with pytest.raises(ValueError):
        record_carry_allocation(db, **{**confirm, 'human_confirmed': False})
    row = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == employee.id,
        LeaveRegisterEntry.work_date == date(2025, 12, 31)))
    row.code = 'U'; db.flush()
    with pytest.raises(ValueError):
        record_carry_allocation(db, **confirm)
    assert db.scalar(select(func.count()).select_from(LeaveCarryAllocation)) == 0


def test_annual_positive_is_not_silently_assigned_a_year_end_cash_mode(prior_year):
    db, employee, kinds, args = prior_year
    with pytest.raises(ValueError, match='separate source-bound'):
        review_carry_allocation(db, **call({**args, 'leave_type_id': kinds['A']}))


@pytest.mark.parametrize('sql', ['UPDATE leave_carry_allocations SET source_year=source_year',
    'DELETE FROM leave_carry_allocations', 'TRUNCATE leave_carry_allocations'])
def test_recorded_carry_is_immutable(prior_year, sql):
    from sqlalchemy.exc import DBAPIError
    db, employee, kinds, args = prior_year
    selection = call(args); review = review_carry_allocation(db, **selection)
    record_carry_allocation(db, operation_id='e'*64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **selection)
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(text(sql))


def test_changed_window_is_not_identical_retry_and_audit_failure_rolls_back(prior_year, monkeypatch):
    from app.models import AuditLog
    db, employee, kinds, args = prior_year
    selection = call(args); review = review_carry_allocation(db, **selection)
    confirm = dict(operation_id='e'*64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **selection)
    original_flush = db.flush
    def refuse_audit(*a, **kw):
        if any(isinstance(row, AuditLog) and row.action == 'leave_carry_allocated' for row in db.new):
            raise RuntimeError('Synthetic audit failure')
        return original_flush(*a, **kw)
    monkeypatch.setattr(db, 'flush', refuse_audit)
    with pytest.raises(RuntimeError, match='Synthetic audit failure'):
        record_carry_allocation(db, **confirm)
    monkeypatch.setattr(db, 'flush', original_flush)
    assert db.scalar(select(func.count()).select_from(LeaveCarryAllocation)) == 0
    record_carry_allocation(db, **confirm)
    changed = {**selection, 'available_through': date(2026, 5, 31)}
    changed_review = review_carry_allocation(db, **changed)
    with pytest.raises(ValueError, match='identity or evidence'):
        record_carry_allocation(db, operation_id='e'*64, reviewed_digest=changed_review['reviewed_digest'],
            human_confirmed=True, **changed)


@pytest.mark.parametrize('balance', ['2.125', '-0.125'])
def test_custom_tracked_type_has_stable_identity_and_no_invented_credit(prior_year, balance):
    from decimal import Decimal
    from app.models import LeaveType
    from app.leave_year_attribution_store import review_year_attribution, apply_year_attribution
    db, employee, kinds, args = prior_year
    kind = LeaveType(code='synthetic_custom_carry', name_ar='Synthetic', name_en='Synthetic', tracks_balance=True, active=True)
    db.add(kind); db.flush()
    ledger = LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=kind.id, transaction_type='opening',
        days_delta=Decimal(balance), reason='Synthetic separately reviewed opening', created_by='synthetic')
    db.add(ledger); db.flush()
    identity = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    attribution = dict(ledger_id=ledger.id, allocations=((2025, Decimal(balance)),),
        reviewed_on=args['reviewed_on'], reason='Synthetic explicit source year', **identity)
    reviewed = review_year_attribution(db, **attribution)
    apply_year_attribution(db, operation_id='a'*64, reviewed_digest=reviewed['reviewed_digest'], human_confirmed=True, **attribution)
    selection = call({**args, 'leave_type_id': kind.id})
    if balance.startswith('-'):
        selection.update(available_from=None, available_through=None)
    reviewed = review_carry_allocation(db, **selection)
    result = record_carry_allocation(db, operation_id='e'*64, reviewed_digest=reviewed['reviewed_digest'], human_confirmed=True, **selection)
    assert Fraction(result['carried_amount']) == Fraction(balance)
    assert result['leave_type_id'] == kind.id and result['ledger_delta'] == '0/1'
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(
        LeaveBalanceTransaction.leave_type_id == kind.id)) == 1
