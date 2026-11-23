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

import re
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


def open_direct_connection(*, autocommit: bool = True):
    """A dedicated psycopg connection to the application database."""
    import psycopg

    url = make_url(settings.database_url)
    connection_string = url.render_as_string(hide_password=False)
    connection_string = connection_string.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg.connect(connection_string, autocommit=autocommit)


# --- HTTP request gate (closes the auth-before/body-after window) -----------
#
# get_current_admin's shared lock ends when authentication commits, so a
# request admitted just before a restore could still run its page-body queries
# against the half-restored database (confirmed by independent review on
# ee74e6e). This gate holds a SHARED transaction-level advisory lock on
# RESTORE_LOCK_KEY from request start until the response finishes, on a
# dedicated connection:
# - requests admitted before a restore hold the shared lock, so the restore's
#   exclusive session lock waits for them to finish on pre-restore data;
# - requests starting mid-restore block until finalization, then see the
#   finalized state (a stale cookie simply 401s after);
# - readers never serialize each other (shared locks are compatible).
#
# Exemptions are exactly the routes whose own transaction takes the EXCLUSIVE
# credential write lock (a middleware-held shared lock on the same key would
# deadlock their upgrade), the restore route itself (it owns the exclusive
# session lock), and pure static/language-switch paths. The pin test scans
# main.py so a new credential-write site without an exemption fails the suite.
GATE_EXEMPT_EXACT = {
    ("POST", "/login"),
    ("GET", "/settings/password"),
    ("POST", "/settings/password"),
    ("POST", "/settings/telegram/bind-confirm"),
    ("POST", "/settings/accounts"),
    ("POST", "/settings/accounts/recovery-codes"),
    ("POST", "/login/recover"),
    ("POST", "/settings/backup/restore"),
}
GATE_EXEMPT_POST_PATTERNS = (
    re.compile(r"^/settings/accounts/[^/]+/(deactivate|activate|reset-password)$"),
    re.compile(r"^/settings/accounts/sessions/[^/]+/end$"),
)
GATE_EXEMPT_PREFIXES = ("/static", "/language")


def restore_gate_exempt(method: str, path: str) -> bool:
    if (method, path) in GATE_EXEMPT_EXACT:
        return True
    if method == "POST" and any(pattern.match(path) for pattern in GATE_EXEMPT_POST_PATTERNS):
        return True
    return any(path.startswith(prefix) for prefix in GATE_EXEMPT_PREFIXES)


class RestoreReadGateMiddleware:
    """Pure ASGI middleware holding the shared restore gate per request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not database_is_postgres():
            await self.app(scope, receive, send)
            return
        if restore_gate_exempt(scope.get("method", "GET"), scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        from starlette.concurrency import run_in_threadpool

        connection = open_direct_connection(autocommit=False)

        def _acquire():
            # Implicit transaction begins here; the xact lock is held until
            # the rollback in _release, i.e. until the response is done.
            connection.execute("SELECT pg_advisory_xact_lock_shared(%s)", (RESTORE_LOCK_KEY,))

        try:
            await run_in_threadpool(_acquire)
        except BaseException:
            connection.close()
            raise
        try:
            await self.app(scope, receive, send)
        finally:
            def _release():
                try:
                    connection.rollback()
                finally:
                    connection.close()

            await run_in_threadpool(_release)


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
