"""Synthetic migrated PostgreSQL preparation transactions, no live hotel data."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.leave_register_preparation import save_reviewed_register_preparation
from app.leave_register_review import RegisterEdit, prepare_register_edit
from app.models import (
    AdminAccount,
    AdminSession,
    AuditLog,
    BackupRun,
    EmployeeServicePeriod,
    LeaveBalanceTransaction,
    LeaveMonthClose,
    LeaveRegisterEntry,
    utcnow,
)
from tests.test_leave_exact_adjustment import adjustment_store  # noqa: F401
from tests.test_leave_precision_migration import precision_schema  # noqa: F401


@pytest.fixture
def preparation_store(adjustment_store):  # noqa: F811
    engine, adjustment, kwargs = adjustment_store
    edit = RegisterEdit(adjustment.employee_id, date(2026, 1, 2), None, None, None,
                        'synthetic_register', date(2026, 1, 1), date(2026, 1, 31))
    with Session(engine) as db, db.begin():
        db.add(EmployeeServicePeriod(employee_id=edit.employee_id, start_date=date(2026, 1, 1),
                                    end_date=None, start_reason='Synthetic'))
    with Session(engine) as db:
        kwargs['reviewed_digest'] = prepare_register_edit(db, edit).source_digest
    return engine, edit, kwargs


def test_unknown_present_update_audit_and_no_ledger_effect(preparation_store):
    engine, edit, kwargs = preparation_store
    first, rev = save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    assert rev == 1
    with Session(engine) as db:
        assert db.get(LeaveRegisterEntry, first).code is None
        unchanged = prepare_register_edit(db, edit)
        assert unchanged.unchanged
    assert save_reviewed_register_preparation(engine, edit=edit,
        **{**kwargs, 'reviewed_digest': unchanged.source_digest}) == (first, 1)
    # Old receipt cannot silently become approval of changed source state.
    with pytest.raises(ValueError, match='stale'):
        save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    present = replace(edit, code='')
    with Session(engine) as db:
        digest = prepare_register_edit(db, present).source_digest
    assert save_reviewed_register_preparation(engine, edit=present,
        **{**kwargs, 'reviewed_digest': digest}) == (first, 2)
    with Session(engine) as db:
        assert db.get(LeaveRegisterEntry, first).code == ''
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == 1
        assert db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == 'leave_register_prepared')) == 2


@pytest.mark.parametrize('change', ['assistant_manager', 'finance', 'inactive', 'reset', 'password', 'version', 'session'])
def test_live_preparer_authority(preparation_store, change):
    engine, edit, kwargs = preparation_store
    with Session(engine) as db, db.begin():
        actor = db.get(AdminAccount, kwargs['actor_id'])
        if change in ('assistant_manager', 'finance'):
            actor.role = change
        elif change == 'inactive':
            actor.active = False
        elif change == 'reset':
            actor.pending_credential_reset = True
        elif change == 'password':
            actor.must_change_password = True
        elif change == 'version':
            actor.session_version += 1
        else:
            db.delete(db.get(AdminSession, kwargs['session_id']))
    if change == 'assistant_manager':
        assert save_reviewed_register_preparation(engine, edit=edit, **kwargs)[1] == 1
    else:
        with pytest.raises(PermissionError):
            save_reviewed_register_preparation(engine, edit=edit, **kwargs)


def test_closure_after_review_refuses_without_reopening(preparation_store):
    engine, edit, kwargs = preparation_store
    with Session(engine) as db, db.begin():
        backup = BackupRun(backup_type='manual', status='failed', size_bytes=0, initiated_by='synthetic')
        db.add(backup)
        db.flush()
        db.add(LeaveMonthClose(year=2026, month=1, backup_run_id=backup.id,
            closed_by='synthetic', closed_at=utcnow(), policy_snapshot_json='{}'))
    with pytest.raises(ValueError, match='closed_month'):
        save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry)) == 0


def test_injected_audit_failure_rolls_back_cell_and_revision(preparation_store):
    engine, edit, kwargs = preparation_store

    def fail(mapper, connection, target):
        raise RuntimeError('Synthetic register audit fault')

    event.listen(AuditLog, 'before_insert', fail)
    try:
        with pytest.raises(RuntimeError, match='audit fault'):
            save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    finally:
        event.remove(AuditLog, 'before_insert', fail)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry)) == 0
    assert save_reviewed_register_preparation(engine, edit=edit, **kwargs)[1] == 1


def test_concurrent_same_review_has_one_success_and_one_stale(preparation_store):
    engine, edit, kwargs = preparation_store

    def attempt():
        try:
            return save_reviewed_register_preparation(engine, edit=edit, **kwargs)
        except ValueError as error:
            assert 'stale' in str(error)
            return 'stale'

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results.count('stale') == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry)) == 1
        assert db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == 'leave_register_prepared')) == 1


def test_stale_revision_after_another_edit_cannot_overwrite(preparation_store):
    engine, edit, kwargs = preparation_store
    first, _ = save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    proposed = replace(edit, code='A')
    with Session(engine) as db:
        digest = prepare_register_edit(db, proposed).source_digest
    competing = replace(edit, code='U')
    with Session(engine) as db:
        other_digest = prepare_register_edit(db, competing).source_digest
    save_reviewed_register_preparation(engine, edit=competing,
        **{**kwargs, 'reviewed_digest': other_digest})
    with pytest.raises(ValueError, match='stale'):
        save_reviewed_register_preparation(engine, edit=proposed,
            **{**kwargs, 'reviewed_digest': digest})
    with Session(engine) as db:
        row = db.get(LeaveRegisterEntry, first)
        assert row.code == 'U' and row.revision == 2


def test_failed_update_preserves_previous_cell_and_revision(preparation_store):
    engine, edit, kwargs = preparation_store
    first, _ = save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    changed = replace(edit, code='S', certificate_attached=True)
    with Session(engine) as db:
        digest = prepare_register_edit(db, changed).source_digest

    def fail(mapper, connection, target):
        raise RuntimeError('Synthetic update audit fault')

    event.listen(AuditLog, 'before_insert', fail)
    try:
        with pytest.raises(RuntimeError, match='audit fault'):
            save_reviewed_register_preparation(engine, edit=changed,
                **{**kwargs, 'reviewed_digest': digest})
    finally:
        event.remove(AuditLog, 'before_insert', fail)
    with Session(engine) as db:
        row = db.get(LeaveRegisterEntry, first)
        assert row.code is None and row.certificate_attached is None and row.revision == 1


@pytest.mark.parametrize('change', ['session', 'closure', 'timeout'])
def test_deterministic_lock_wait_rechecks_source_and_authority(preparation_store, change):
    from threading import Event

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    engine, edit, kwargs = preparation_store
    reached = Event()

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('LOCK TABLE leave_register_entries,'):
            reached.set()

    blocker = engine.connect()
    transaction = blocker.begin()
    blocker.execute(text('LOCK TABLE leave_register_entries IN SHARE ROW EXCLUSIVE MODE'))
    event.listen(engine, 'before_cursor_execute', observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(save_reviewed_register_preparation, engine, edit=edit, **kwargs)
            assert reached.wait(3), 'Writer did not reach the deterministic lock boundary.'
            if change == 'timeout':
                with pytest.raises(DBAPIError, match='lock timeout'):
                    future.result(timeout=8)
            else:
                if change == 'session':
                    blocker.execute(text('DELETE FROM admin_sessions WHERE id=:id'), {'id': kwargs['session_id']})
                else:
                    backup = blocker.execute(text("INSERT INTO backup_runs(backup_type,status,size_bytes,initiated_by,created_at) "
                        "VALUES ('manual','failed',0,'synthetic',now()) RETURNING id")).scalar_one()
                    blocker.execute(text("INSERT INTO leave_month_close(year,month,backup_run_id,closed_by,closed_at,policy_snapshot_json) "
                        "VALUES(2026,1,:id,'synthetic',now(),'{}')"), {'id': backup})
                transaction.commit()
                with pytest.raises(PermissionError if change == 'session' else ValueError):
                    future.result(timeout=8)
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()
        event.remove(engine, 'before_cursor_execute', observe)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry)) == 0
        assert db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == 'leave_register_prepared')) == 0


def test_database_deferred_audit_failure_at_commit_rolls_back(preparation_store):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    engine, edit, kwargs = preparation_store
    with engine.begin() as connection:
        connection.execute(text('''CREATE FUNCTION synthetic_register_commit_fault() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'Synthetic deferred audit fault'; END; $$ LANGUAGE plpgsql'''))
        connection.execute(text('''CREATE CONSTRAINT TRIGGER synthetic_register_commit_fault
            AFTER INSERT ON audit_logs DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION synthetic_register_commit_fault()'''))
    with pytest.raises(DBAPIError, match='deferred audit fault'):
        save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry)) == 0
        assert db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == 'leave_register_prepared')) == 0
