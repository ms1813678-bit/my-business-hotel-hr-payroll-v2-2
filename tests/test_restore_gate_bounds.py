"""Bounded, cancellable read-gate admission (bc74a88 follow-up).

Peer finding: a long restore parked one dedicated connection per gated
request with no bound, and disconnects during the wait were not honored
until the lock granted. The gate must cap its connections per worker,
abort within the deadline with a plain 503, and release everything when a
client goes away mid-wait.
"""
import asyncio

import pytest

from app import restore_lock


class _FakeCursor:
    def __init__(self, granted):
        self._granted = granted

    def fetchone(self):
        return (self._granted,)


class _FakeConnection:
    def __init__(self, granted=False):
        self.granted = granted
        self.closed = False
        self.rolled_back = False

    def execute(self, *args, **kwargs):
        return _FakeCursor(self.granted)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture()
def gate_env(monkeypatch):
    monkeypatch.setattr(restore_lock, "database_is_postgres", lambda: True)
    monkeypatch.setattr(restore_lock, "restore_gate_exempt", lambda method, path: False)
    monkeypatch.setattr(restore_lock, "GATE_ACQUIRE_TIMEOUT", 0.6)
    monkeypatch.setattr(restore_lock, "GATE_POLL_INTERVAL", 0.05)
    restore_lock._gate_active = 0
    yield
    restore_lock._gate_active = 0


def _scope():
    return {"type": "http", "method": "GET", "path": "/employees"}


def test_disconnect_while_waiting_releases_slot_and_connection(gate_env, monkeypatch):
    conn = _FakeConnection(granted=False)
    monkeypatch.setattr(restore_lock, "open_direct_connection", lambda autocommit=False: conn)
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def downstream(scope, receive, send):  # pragma: no cover - must not run
        raise AssertionError("downstream must not run after disconnect")

    asyncio.run(restore_lock.RestoreReadGateMiddleware(downstream)(_scope(), receive, send))
    # A response is STILL sent even though the client is gone: returning
    # silently makes the outer BaseHTTPMiddleware layers raise an unhandled
    # NoResponseReturned (peer finding on 4a7d778).
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 503
    assert conn.closed and conn.rolled_back
    assert restore_lock._gate_active == 0


def test_timeout_returns_503_and_releases(gate_env, monkeypatch):
    conn = _FakeConnection(granted=False)
    monkeypatch.setattr(restore_lock, "open_direct_connection", lambda autocommit=False: conn)
    sent = []

    async def receive():
        await asyncio.Event().wait()  # client silent; every poll times out

    async def send(message):
        sent.append(message)

    asyncio.run(restore_lock.RestoreReadGateMiddleware(None)(_scope(), receive, send))
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 503
    assert conn.closed and conn.rolled_back
    assert restore_lock._gate_active == 0


def test_admission_cap_rejects_beyond_limit_without_opening_connection(gate_env, monkeypatch):
    monkeypatch.setattr(restore_lock, "GATE_MAX_WAITERS", 1)
    restore_lock._gate_active = 1  # one slot already held by another request
    opened = []
    monkeypatch.setattr(
        restore_lock, "open_direct_connection",
        lambda autocommit=False: opened.append(1) or _FakeConnection(),
    )
    sent = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    asyncio.run(restore_lock.RestoreReadGateMiddleware(None)(_scope(), receive, send))
    assert sent[0]["status"] == 503
    assert opened == []  # never opened a connection past the cap
    assert restore_lock._gate_active == 1  # the pre-existing holder is untouched


def test_granted_lock_serves_and_releases(gate_env, monkeypatch):
    conn = _FakeConnection(granted=True)
    monkeypatch.setattr(restore_lock, "open_direct_connection", lambda autocommit=False: conn)
    sent = []
    served = []

    async def receive():
        await asyncio.Event().wait()  # pragma: no cover - no body expected

    async def send(message):
        sent.append(message)

    async def downstream(scope, receive, send):
        served.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    asyncio.run(restore_lock.RestoreReadGateMiddleware(downstream)(_scope(), receive, send))
    assert served == [True]
    assert sent[0]["status"] == 200
    assert conn.closed and conn.rolled_back
    assert restore_lock._gate_active == 0


def test_failed_connect_releases_slot_and_gate_keeps_working(gate_env, monkeypatch):
    """A DB that is down at connect time must not leak gate slots (16 leaks would 503 everything)."""
    attempts = {"n": 0}

    def failing(autocommit=False):
        attempts["n"] += 1
        raise RuntimeError("database down")

    monkeypatch.setattr(restore_lock, "open_direct_connection", failing)

    async def receive():
        await asyncio.Event().wait()  # pragma: no cover

    async def send(message):  # pragma: no cover - the error propagates, nothing is sent
        raise AssertionError("no response expected from the gate on a connect error")

    async def downstream(scope, receive, send):  # pragma: no cover
        raise AssertionError("downstream must not run when the gate cannot connect")

    for _ in range(restore_lock.GATE_MAX_WAITERS + 1):
        with pytest.raises(RuntimeError):  # fail closed: still an error, not an open gate
            asyncio.run(restore_lock.RestoreReadGateMiddleware(downstream)(_scope(), receive, send))
        assert restore_lock._gate_active == 0
    assert attempts["n"] == restore_lock.GATE_MAX_WAITERS + 1

    conn = _FakeConnection(granted=True)
    monkeypatch.setattr(restore_lock, "open_direct_connection", lambda autocommit=False: conn)
    sent, served = [], []

    async def send_ok(message):
        sent.append(message)

    async def downstream_ok(scope, receive, send):
        served.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    asyncio.run(restore_lock.RestoreReadGateMiddleware(downstream_ok)(_scope(), receive, send_ok))
    assert served == [True] and sent[0]["status"] == 200
    assert conn.closed and restore_lock._gate_active == 0
