"""Restore write-exclusion: lock helpers are inert off PostgreSQL, and the
restore window plus every credential-mutating path stay wired to one key."""
import inspect

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import backup_workflow, restore_lock
from app.db import Base


def _sqlite_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_lock_helpers_noop_on_sqlite(monkeypatch):
    monkeypatch.setattr(restore_lock, "database_is_postgres", lambda: False)
    db = _sqlite_session()
    try:
        restore_lock.acquire_credential_write_lock(db)  # must not raise
        with restore_lock.hold_restore_lock() as connection:
            assert connection is None
    finally:
        db.close()


def test_restore_holds_one_session_lock_across_swap_and_finalize():
    source = inspect.getsource(backup_workflow.restore_backup)
    assert "hold_restore_lock" in source
    assert "swap_connection=lock_connection" in source
    # The lock must wrap the finalize AND the rollback restore, not just the
    # snapshot: both _restore_archive_contents calls sit inside the context.
    assert source.index("hold_restore_lock") < source.index("_finalize_restored_security")
    assert source.index("_restore_archive_contents(pre_restore_path") > source.index("hold_restore_lock")
    assert "pg_advisory_lock" in inspect.getsource(restore_lock.hold_restore_lock)


def test_schema_swap_runs_on_the_lock_connection():
    """The terminate query excludes the backend it executes on, so running it
    on the lock connection keeps the lock alive; a second connection would be
    terminated and the lock silently lost."""
    source = inspect.getsource(backup_workflow._restore_archive_contents)
    assert "_replace_public_schema(swap_connection)" in source
    terminate = inspect.getsource(backup_workflow._replace_public_schema)
    assert "pid <> pg_backend_pid()" in terminate


def test_every_credential_write_path_takes_the_xact_lock():
    import app.main as main

    source = inspect.getsource(main)
    sites = source.count("acquire_credential_write_lock(db")
    # login, reauth clear/record, telegram save/link/confirm, change password
    # (x2), create, deactivate, activate, end session, reset password,
    # recovery codes, recovery.
    assert sites >= 15
    lock_source = inspect.getsource(restore_lock.acquire_credential_write_lock)
    assert "pg_advisory_xact_lock" in lock_source
    assert "RESTORE_LOCK_KEY" in lock_source
