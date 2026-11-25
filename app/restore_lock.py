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

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.config import settings

# One fixed signed-int64 key shared by the restore lock and every credential
# write lock; the two sides must collide on the same key to exclude each
# other.
RESTORE_LOCK_KEY = 72727272901138

# SystemSetting key holding the current restore epoch. Every successful
# restore bumps it, so a request authenticated against the pre-restore
# database (whose numeric account ids may now belong to DIFFERENT users) is
# refused at the credential write lock instead of mutating the wrong account.
RESTORE_EPOCH_KEY = "restore_epoch"


def read_restore_epoch(db) -> str | None:
    """Current restore epoch, or None when never restored / not PostgreSQL."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return None
    from app.models import SystemSetting

    return db.scalar(select(SystemSetting.value).where(SystemSetting.key == RESTORE_EPOCH_KEY))


def database_is_postgres() -> bool:
    return make_url(settings.database_url).get_backend_name() == "postgresql"


# Sentinel distinguishing "pre-authentication site, no epoch check" from an
# authenticated request whose database had no epoch row yet (None). None is a
# REAL epoch: after the first restore it no longer matches, so the write is
# refused instead of mutating by a stale numeric id.
_NO_EPOCH_CHECK = object()


def acquire_credential_write_lock(db, *, expected_epoch=_NO_EPOCH_CHECK) -> None:
    """Block this transaction until no restore is in progress.

    Takes a transaction-level advisory lock: it waits while a restore holds
    the session-level lock, then holds the xact-level lock until this
    transaction ends, which makes a newly starting restore wait for this
    write to commit or roll back first.

    When expected_epoch is given (authenticated requests carry the epoch
    observed at authentication time on request.state.restore_epoch), a
    mismatch means a restore finalized between authentication and this
    write, so fail closed with 409 instead of mutating by a stale identity.
    Pre-authentication sites (login, recovery, reauth throttle counters)
    omit expected_epoch: they re-read the account by username inside the
    locked transaction, which is safe because usernames are carried over
    unchanged. Authenticated sites always pass request.state.restore_epoch,
    including None (database never restored): after the first restore that
    None mismatches and the write is refused.
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": RESTORE_LOCK_KEY})
    if expected_epoch is not _NO_EPOCH_CHECK and read_restore_epoch(db) != expected_epoch:
        raise HTTPException(
            status_code=409,
            detail="اكتمل استرجاع نسخة احتياطية أثناء تنفيذ الطلب، راجع البيانات وأعد المحاولة.",
        )


def acquire_authenticated_read_lock(db) -> None:
    """Shared gate for authentication/authorization reads (get_current_admin).

    Concurrent requests share the lock, so normal traffic is NOT serialized;
    a restore's exclusive session-level lock makes every authenticated read
    refuse promptly while finalization (or rollback) owns or awaits the gate, so a stale cookie can
    never resolve identity, role or session rows from the half-restored
    window. The caller must end the transaction right after authentication
    (get_current_admin commits) so the shared lock is not held for the whole
    request — that would deadlock the restore route against its own
    authentication.
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # A request may already own the middleware shared gate on another
    # connection. Never queue a second holder behind an exclusive restore
    # waiter: refuse promptly so the first holder can release its lock.
    acquired = db.scalar(text("SELECT pg_try_advisory_xact_lock_shared(:key)"), {"key": RESTORE_LOCK_KEY})
    if not acquired:
        raise HTTPException(status_code=503,
            detail="النظام مشغول مؤقتًا، أعد المحاولة بعد قليل.",
            headers={"Retry-After": "5"})


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
    ("POST", "/leave/opening/review"),
    ("POST", "/leave/departments/export"),
    ("POST", "/leave/departments/return"),
    ("POST", "/leave/departments/compare"),
    ("POST", "/leave/policies/preview"),
    ("POST", "/leave/policies/confirm"),
    ("POST", "/leave/policies/impact"),
    ("POST", "/leave/balance-explanation"),
    ("POST", "/leave/opening/policy-defaults"),
    ("POST", "/leave/opening/confirm-source"),
    ("POST", "/settings/password"),
    ("POST", "/settings/telegram"),
    ("POST", "/settings/telegram/bind-link"),
    ("POST", "/settings/telegram/bind-confirm"),
    ("POST", "/settings/telegram/test"),
    ("POST", "/settings/accounts"),
    ("POST", "/settings/accounts/recovery-codes"),
    ("POST", "/login/recover"),
    ("POST", "/settings/backup/restore"),
}
GATE_EXEMPT_POST_PATTERNS = (
    re.compile(r'/leave/carry-consumption/[1-9][0-9]*/(?:review|confirm)'),
    re.compile(r'/leave/annual-settlement/[0-9a-f]{64}/(?:review|confirm)'),
    # These POST handlers acquire their own bounded exclusive gate before all source work.
    re.compile(r'/leave/cash-out/[1-9][0-9]*/(?:review|confirm)'),
    re.compile(r'/payroll/items/[1-9][0-9]*/overtime-days/(?:review|confirm|monthly)'),
    re.compile(r'/leave/month-review/register-(?:review|confirm)'),
    # Exact monthly routes acquire their own bounded exclusive gate through
    # _locks/_guard before source reads and retain it through commit/render.
    re.compile(r'/leave/month-review/earning/[0-9a-f]{64}/[1-9][0-9]*/(?:review|confirm)'),
    re.compile(r'/leave/month-review/reconciliation-(?:snapshot|draft|record|approve)'),
    re.compile(r'/attendance/shift-evidence/[0-9a-f]{64}/[1-9][0-9]*/(?:review|confirm)'),
    re.compile(r"^/settings/accounts/[^/]+/(deactivate|activate|reset-password)$"),
    re.compile(r"^/settings/accounts/sessions/[^/]+/end$"),
)
GATE_EXEMPT_PREFIXES = ("/static", "/language")


def restore_gate_exempt(method: str, path: str) -> bool:
    if (method, path) in GATE_EXEMPT_EXACT:
        return True
    # Approval preview revalidates the exact source under its own bounded
    # exclusive transaction gate; middleware must not hold a shared gate.
    if method == "GET" and re.fullmatch(r'/leave/month-review/reconciliation-approval/[0-9a-f]{64}',path):
        return True
    if method == "POST" and any(pattern.fullmatch(path) for pattern in GATE_EXEMPT_POST_PATTERNS):
        return True
    return any(path.startswith(prefix) for prefix in GATE_EXEMPT_PREFIXES)


# Bounded, cancellable gate admission (peer finding on bc74a88): every gated
# request holds a DEDICATED connection, so a long restore must not be able to
# park unbounded connections/locks on the database. At most
# GATE_MAX_WAITERS gate connections exist per worker process; while waiting
# (for a slot or for the lock) the middleware polls for client disconnect
# and aborts within GATE_ACQUIRE_TIMEOUT seconds with a plain 503, closing
# the connection immediately. Try-lock polling replaces the blocking call so
# a cancelled waiter never lingers until the restore finishes.
GATE_MAX_WAITERS = 16
GATE_ACQUIRE_TIMEOUT = 30.0
GATE_POLL_INTERVAL = 0.15

_gate_active = 0


async def _poll_disconnect(receive, buffered: list, interval: float) -> bool:
    """True when the client went away; other messages are buffered for replay."""
    import asyncio

    try:
        message = await asyncio.wait_for(receive(), timeout=interval)
    except asyncio.TimeoutError:
        return False
    if message["type"] == "http.disconnect":
        return True
    buffered.append(message)
    return False


async def _send_gate_unavailable(send) -> None:
    body = "استرجاع نسخة احتياطية جارٍ، أعد المحاولة بعد قليل.".encode("utf-8")
    await send({"type": "http.response.start", "status": 503, "headers": [
        (b"content-type", b"text/plain; charset=utf-8"), (b"retry-after", b"5"),
        (b"content-length", str(len(body)).encode()),
    ]})
    await send({"type": "http.response.body", "body": body})


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
        import time

        from starlette.concurrency import run_in_threadpool

        global _gate_active
        buffered: list = []
        deadline = time.monotonic() + GATE_ACQUIRE_TIMEOUT

        # Phase 1: wait for a bounded gate slot (no connection held yet).
        # A disconnect must STILL send a response: the language/security
        # BaseHTTPMiddleware layers outside this gate raise an unhandled
        # NoResponseReturned when the inner app returns silently (peer
        # finding on 4a7d778). Sending to the dead client is harmless — the
        # middleware wrappers swallow it — and the resources are released.
        while _gate_active >= GATE_MAX_WAITERS:
            if time.monotonic() >= deadline:
                await _send_gate_unavailable(send)
                return
            if await _poll_disconnect(receive, buffered, GATE_POLL_INTERVAL):
                await _send_gate_unavailable(send)
                return
        _gate_active += 1
        connection = open_direct_connection(autocommit=False)
        acquired = False
        try:
            # Phase 2: cancellable try-lock loop on the shared gate.
            while True:
                def _try_lock():
                    cursor = connection.execute(
                        "SELECT pg_try_advisory_xact_lock_shared(%s)", (RESTORE_LOCK_KEY,))
                    return cursor.fetchone()[0]

                if await run_in_threadpool(_try_lock):
                    acquired = True
                    break
                if time.monotonic() >= deadline:
                    await _send_gate_unavailable(send)
                    return
                if await _poll_disconnect(receive, buffered, GATE_POLL_INTERVAL):
                    await _send_gate_unavailable(send)
                    return

            async def replay_receive():
                if buffered:
                    return buffered.pop(0)
                return await receive()

            await self.app(scope, replay_receive, send)
        finally:
            def _release():
                try:
                    connection.rollback()
                finally:
                    connection.close()

            await run_in_threadpool(_release)
            _gate_active -= 1


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
