"""Advisory-lock coordination between backup restores and credential writes.

A restore holds a session-level PostgreSQL advisory lock from before the
security-state snapshot until the post-restore finalization (or the rollback)
finishes. Every credential-mutating transaction first takes a
transaction-level advisory lock on the same key, so a credential write either
completes before the restore snapshot is captured or waits until the restore
is fully done; archived credentials are never usable in between, and no
credential write can land in the half-restored window. The session-level lock
is released automatically when its connection closes, including on a crash.

On non-PostgreSQL backends (unit tests) both helpers are no-ops.
"""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings

# One fixed signed-int64 key shared by the restore lock and every credential
# write lock; the two sides must collide on the same key to exclude each
# other.
RESTORE_LOCK_KEY = 72727272901138


def database_is_postgres() -> bool:
    return make_url(settings.database_url).get_backend_name() == "postgresql"


def acquire_credential_write_lock(db) -> None:
    """Block this transaction until no restore is in progress.

    Takes a transaction-level advisory lock: it waits while a restore holds
    the session-level lock, then holds the xact-level lock until this
    transaction ends, which makes a newly starting restore wait for this
    write to commit or roll back first.
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": RESTORE_LOCK_KEY})


def acquire_authenticated_read_lock(db) -> None:
    """Shared gate for authentication/authorization reads (get_current_admin).

    Concurrent requests share the lock, so normal traffic is NOT serialized;
    a restore's exclusive session-level lock makes every authenticated read
    wait until finalization (or rollback) completes, so a stale cookie can
    never resolve identity, role or session rows from the half-restored
    window. The caller must end the transaction right after authentication
    (get_current_admin commits) so the shared lock is not held for the whole
    request — that would deadlock the restore route against its own
    authentication.
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": RESTORE_LOCK_KEY})


def open_direct_connection():
    """A dedicated autocommit psycopg connection to the application database."""
    import psycopg

    url = make_url(settings.database_url)
    connection_string = url.render_as_string(hide_password=False)
    connection_string = connection_string.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg.connect(connection_string, autocommit=True)


@contextmanager
def hold_restore_lock():
    """Hold the session-level restore lock for the whole restore window.

    Yields the dedicated connection so the schema swap can run its
    pg_terminate_backend through it: the termination query already excludes
    the backend it executes on, which keeps this connection (and the lock it
    holds) alive while every other backend is dropped. A blocking lock is
    used on purpose: short credential transactions holding the xact-level
    lock drain first, and a second restore queues behind this one instead of
    racing the schema swap.
    """
    if not database_is_postgres():
        yield None
        return
    connection = open_direct_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (RESTORE_LOCK_KEY,))
        yield connection
    finally:
        connection.close()
