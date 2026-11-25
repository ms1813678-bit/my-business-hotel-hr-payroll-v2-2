"""Migrated PostgreSQL exact adjustment kernel tests. Synthetic data only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.leave_exact_adjustment import (
    ExactAdjustment,
    adjustment_evidence_digest,
    apply_reviewed_exact_adjustment,
)
from app.models import AdminAccount, AdminSession, AuditLog, LeaveBalanceTransaction
from tests.test_leave_precision_migration import migrate, precision_schema, seed_legacy  # noqa: F401
from tests.sync_uid_test_support import discard_sync_uid_for_older_downgrade


@pytest.fixture
def adjustment_store(precision_schema):  # noqa: F811
    with precision_schema.begin() as connection:
        migrate(connection, 'head')
        seed_legacy(connection, Decimal('2.00'))
        employee_id, type_id = connection.execute(text(
            'SELECT employee_id,leave_type_id FROM leave_balance_transactions')).one()
    with Session(precision_schema) as db, db.begin():
        actor = AdminAccount(username='synthetic-exact-hr', password_hash='not-a-login',
            role='system_admin', active=True, session_version=0)
        db.add(actor)
        db.flush()
        actor_id = actor.id
        db.add(AdminSession(id='synthetic-exact-session', admin_account_id=actor_id))
    proposal = ExactAdjustment(employee_id=employee_id, leave_type_id=type_id,
        days_delta=Decimal('-3.14285714285714285714285714285714285714285714285714'),
        reason='Synthetic reviewed manual correction', operation_key='synthetic-source-001',
        earning_year=2025, released_on=date(2026, 2, 1))
    with Session(precision_schema) as db:
        digest = adjustment_evidence_digest(db, proposal)
    yield precision_schema, proposal, {'actor_id': actor_id, 'session_id': 'synthetic-exact-session',
        'session_version': 0, 'reviewed_digest': digest}


def test_exact_adjustment_atomic_idempotent_and_negative(adjustment_store):
    engine, proposal, kwargs = adjustment_store
    first = apply_reviewed_exact_adjustment(engine, proposal=proposal, **kwargs)
    assert apply_reviewed_exact_adjustment(engine, proposal=proposal, **kwargs) == first
    with Session(engine) as db:
        entry = db.get(LeaveBalanceTransaction, first)
        assert entry.days_delta == proposal.days_delta
        assert entry.earning_year == 2025 and entry.released_on == date(2026, 2, 1)
        assert db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == 'leave_exact_manual_adjustment')) == 1
        assert db.scalar(text('SELECT sum(days_delta) FROM leave_balance_transactions')) < 0
    for field in ('days_delta', 'reason', 'earning_year', 'released_on', 'employee_id'):
        changed = {'days_delta': Decimal(1), 'reason': 'Different synthetic reason',
                       'earning_year': 2026, 'released_on': date(2026, 3, 1), 'employee_id': 2147483647}
        with pytest.raises(ValueError, match='conflicts'):
            apply_reviewed_exact_adjustment(engine, proposal=replace(proposal, **{field: changed[field]}), **kwargs)


@pytest.mark.parametrize('change', ['finance', 'assistant_manager', 'inactive', 'reset', 'password', 'version', 'session'])
def test_current_operator_and_session_rechecked(adjustment_store, change):
    engine, proposal, kwargs = adjustment_store
    with Session(engine) as db, db.begin():
        actor = db.get(AdminAccount, kwargs['actor_id'])
        if change in ('finance', 'assistant_manager'):
            actor.role = change
        elif change == 'inactive':
            actor.active = False
        elif change == 'reset':
            actor.pending_credential_reset = True
        elif change == 'password':
            actor.must_change_password = True
        elif change == 'version':
            actor.session_version = 1
        else:
            db.delete(db.get(AdminSession, kwargs['session_id']))
    with pytest.raises(PermissionError):
        apply_reviewed_exact_adjustment(engine, proposal=proposal, **kwargs)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == 1


def test_stale_balance_and_dirty_review_session_never_commit(adjustment_store):
    engine, proposal, kwargs = adjustment_store
    with Session(engine) as db:
        actor = db.get(AdminAccount, kwargs['actor_id'])
        actor.role = 'finance'
        assert adjustment_evidence_digest(db, proposal) == kwargs['reviewed_digest']
        assert actor in db.dirty
        db.rollback()
    with engine.begin() as connection:
        connection.execute(text("UPDATE leave_balance_transactions SET days_delta=3"))
    with pytest.raises(ValueError, match='stale'):
        apply_reviewed_exact_adjustment(engine, proposal=proposal, **kwargs)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == 1


def test_audit_failure_rolls_back_ledger_and_source_slot(adjustment_store):
    engine, proposal, kwargs = adjustment_store

    def fail(mapper, connection, target):
        raise RuntimeError('Synthetic audit fault')

    event.listen(AuditLog, 'before_insert', fail)
    try:
        with pytest.raises(RuntimeError, match='Synthetic audit fault'):
            apply_reviewed_exact_adjustment(engine, proposal=proposal, **kwargs)
    finally:
        event.remove(AuditLog, 'before_insert', fail)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == 1
    assert apply_reviewed_exact_adjustment(engine, proposal=proposal, **kwargs)


def test_concurrent_same_operation_cannot_double_credit(adjustment_store):
    engine, proposal, kwargs = adjustment_store
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(apply_reviewed_exact_adjustment, engine,
            proposal=proposal, **kwargs) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == 2
        assert db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == 'leave_exact_manual_adjustment')) == 1


@pytest.mark.parametrize('delta', [True, 1, 1.1, Decimal(0), Decimal('NaN'), Decimal('Infinity')])
def test_invalid_amount_rejected_before_database_access(delta):
    proposal = ExactAdjustment(1, 1, delta, 'Synthetic', 'synthetic', 2026, date(2026, 1, 1))
    with pytest.raises(ValueError):
        adjustment_evidence_digest(None, proposal)


def test_source_history_is_immutable_but_legacy_rows_unchanged(adjustment_store):
    from sqlalchemy.exc import DBAPIError

    engine, proposal, kwargs = adjustment_store
    entry_id = apply_reviewed_exact_adjustment(engine, proposal=proposal, **kwargs)
    for statement in (
            'UPDATE leave_balance_transactions SET days_delta=9 WHERE id=:id',
            'UPDATE leave_balance_transactions SET source_operation_key=NULL WHERE id=:id',
            'DELETE FROM leave_balance_transactions WHERE id=:id'):
        with pytest.raises(DBAPIError, match='immutable'), engine.begin() as connection:
            connection.execute(text(statement), {'id': entry_id})
    with engine.begin() as connection:
        connection.execute(text('UPDATE leave_balance_transactions SET reason=:reason '
                                'WHERE source_operation_key IS NULL'), {'reason': 'Legacy behavior preserved'})
    with engine.begin() as connection:
        discard_sync_uid_for_older_downgrade(connection)
    with engine.begin() as connection, pytest.raises(RuntimeError, match='immutability'):
        migrate(connection, '0045_leave_source_identity', down=True)
    with engine.begin() as connection:
        migrate(connection, 'head')
    assert apply_reviewed_exact_adjustment(engine, proposal=proposal, **kwargs) == entry_id


def test_review_never_overwrites_pending_employee_or_ledger_changes(adjustment_store):
    from app.models import Employee

    engine, proposal, kwargs = adjustment_store
    with Session(engine) as db:
        employee = db.get(Employee, proposal.employee_id)
        entry = db.scalar(select(LeaveBalanceTransaction))
        employee.full_name = 'Pending synthetic name'
        entry.days_delta = Decimal(999)
        assert adjustment_evidence_digest(db, proposal) == kwargs['reviewed_digest']
        assert employee.full_name == 'Pending synthetic name'
        assert entry.days_delta == Decimal(999)
        assert employee in db.dirty and entry in db.dirty


def test_competing_different_operations_require_new_review(adjustment_store):
    engine, proposal, kwargs = adjustment_store
    second = replace(proposal, operation_key='synthetic-source-002')
    with Session(engine) as db:
        second_digest = adjustment_evidence_digest(db, second)
    apply_reviewed_exact_adjustment(engine, proposal=proposal, **kwargs)
    with pytest.raises(ValueError, match='stale'):
        apply_reviewed_exact_adjustment(engine, proposal=second,
            **{**kwargs, 'reviewed_digest': second_digest})
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == 2
