# ruff: noqa: F811
"""Stored carry retries must not re-derive against later legitimate ledger growth."""
from decimal import Decimal
import pytest
from sqlalchemy import func, select
from app.leave_carry_allocation_store import review_carry_allocation, record_carry_allocation
from app.models import LeaveBalanceTransaction, LeaveCarryAllocation
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_leave_carry_allocation_store import call


def recorded(prior_year):
    db, employee, kinds, args = prior_year
    selection = call(args)
    review = review_carry_allocation(db, **selection)
    confirm = dict(operation_id='e'*64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **selection)
    first = record_carry_allocation(db, **confirm)
    return db, employee, kinds, args, confirm, first


def test_honest_retry_after_later_ledger_growth_no_new_receipt_or_credit(prior_year):
    db, employee, kinds, args, confirm, first = recorded(prior_year)
    db.add(LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=kinds['W'],
        transaction_type='leave_use', days_delta=Decimal(-2), reason='Synthetic later debit', created_by='synthetic'))
    db.flush()
    count = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    assert record_carry_allocation(db, **confirm) == first
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == count
    assert db.scalar(select(func.count()).select_from(LeaveCarryAllocation)) == 1
    with pytest.raises(ValueError):
        record_carry_allocation(db, **{**confirm, 'operation_id': 'f'*64})


@pytest.mark.parametrize('change', ['selection', 'reason', 'window', 'review_date', 'digest', 'actor', 'session', 'version', 'epoch', 'insurance', 'age'])
def test_changed_retry_identity_refuses_without_derive(prior_year, monkeypatch, change):
    from datetime import date
    import app.leave_carry_allocation_store as store
    db, employee, kinds, args, confirm, first = recorded(prior_year)
    selection = dict(confirm['source_selection']); confirm = {**confirm, 'source_selection': selection}
    if change == 'selection': selection['selection_id'] = 'f'*64
    elif change == 'reason': confirm['reason'] = 'Changed reason'
    elif change == 'window': confirm['available_through'] = date(2026, 5, 31)
    elif change == 'review_date': selection['reviewed_on'] = date(2026, 1, 3)
    elif change == 'digest': confirm['reviewed_digest'] = 'f'*64
    elif change == 'actor': selection['actor_id'] += 10000
    elif change == 'session': selection['session_id'] = 'other-session'
    elif change == 'version': selection['session_version'] += 1
    elif change == 'epoch': selection['expected_epoch'] = 'wrong-epoch'
    elif change == 'insurance': selection['current_insurance_years'] = {}
    elif change == 'age': selection['age_evaluation_dates'] = {}
    def no_derive(*a, **k): raise AssertionError('Existing operation must be verified without fresh derivation')
    monkeypatch.setattr(store, '_derive', no_derive)
    with pytest.raises(ValueError): record_carry_allocation(db, **confirm)


@pytest.mark.parametrize('change', ['ledger', 'audit'])
def test_retry_checks_stored_proof_not_only_request(prior_year, change):
    from sqlalchemy import text
    db, employee, kinds, args, confirm, first = recorded(prior_year)
    table = 'audit_logs' if change == 'audit' else 'leave_balance_transactions'
    db.execute(text(f'ALTER TABLE {table} DISABLE TRIGGER USER'))
    try:
        if change == 'audit':
            db.execute(text("UPDATE audit_logs SET reason='Changed audit' WHERE action='leave_carry_allocated'"))
        else:
            db.execute(text("UPDATE leave_balance_transactions SET source_digest=repeat('f',64) WHERE employee_id=:id"), {'id': employee.id})
    finally:
        db.execute(text(f'ALTER TABLE {table} ENABLE TRIGGER USER'))
    db.expire_all()
    with pytest.raises(ValueError): record_carry_allocation(db, **confirm)


@pytest.mark.parametrize('who', ['same_actor_new_session', 'other_valid_actor'])
def test_other_authenticated_session_cannot_replay_original_operation(prior_year, who):
    from app.models import AdminAccount, AdminSession
    db, employee, kinds, args, confirm, first = recorded(prior_year)
    account = db.get(AdminAccount, args['actor_id'])
    if who == 'other_valid_actor':
        account = AdminAccount(username='other-valid-hr', password_hash='synthetic', active=True, role='system_admin')
        db.add(account); db.flush()
    session = AdminSession(id='other-valid-session', admin_account_id=account.id)
    db.add(session); db.flush()
    selection = {**confirm['source_selection'], 'actor_id': account.id, 'session_id': session.id,
                 'session_version': account.session_version}
    with pytest.raises(ValueError, match='identity or evidence'):
        record_carry_allocation(db, **{**confirm, 'source_selection': selection})


def test_legacy_receipt_without_exact_input_map_keeps_strict_old_path(prior_year):
    import json
    from sqlalchemy import text
    from app.leave_month_earning import canonical, digest
    db, employee, kinds, args, confirm, first = recorded(prior_year)
    row = db.get(LeaveCarryAllocation, confirm['operation_id'])
    request = json.loads(row.request_json); request.pop('source_selection')
    db.execute(text('ALTER TABLE leave_carry_allocations DISABLE TRIGGER USER'))
    try:
        db.execute(text('UPDATE leave_carry_allocations SET request_json=:q,request_digest=:d WHERE id=:id'),
            {'q': canonical(request), 'd': digest(request), 'id': row.id})
    finally:
        db.execute(text('ALTER TABLE leave_carry_allocations ENABLE TRIGGER USER'))
    db.expire_all()
    assert json.loads(canonical(record_carry_allocation(db, **confirm))) == first
    db.add(LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=kinds['W'],
        transaction_type='leave_use', days_delta=Decimal(-2), reason='Synthetic later debit', created_by='synthetic'))
    db.flush()
    with pytest.raises(ValueError): record_carry_allocation(db, **confirm)
