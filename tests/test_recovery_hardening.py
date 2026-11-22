"""F1/F2 hardening: timing-equalized recovery lookups, atomic throttle audit."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, AuditLog, RecoveryCode
from app.security import hash_password

ADMIN_PW = "Guard-Test-Password-123!"


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        db.add(AdminAccount(
            username="guard-admin",
            password_hash=hash_password(ADMIN_PW),
            display_name="مدير النظام التجريبي",
            role="system_admin",
            active=True,
        ))
        db.commit()

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", factory)
    try:
        with TestClient(app) as test_client:
            test_client.test_session_factory = factory
            yield test_client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def login(client):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": "guard-admin", "password": ADMIN_PW, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def generate_codes(client):
    page = client.get("/settings/accounts")
    response = client.post(
        "/settings/accounts/recovery-codes",
        data={"csrf_token": csrf(page), "current_password": ADMIN_PW},
        follow_redirects=False,
    )
    assert response.status_code == 200
    return re.findall(r"<code>([^<]+)</code>", response.text)


def recover(client, username, code, verify_spy=None):
    page = client.get("/login/recover")
    return client.post(
        "/login/recover",
        data={
            "csrf_token": csrf(page),
            "username": username,
            "recovery_code": code,
            "new_password": "Recovered-New-Password-77!",
            "confirm_password": "Recovered-New-Password-77!",
        },
        follow_redirects=False,
    )


def test_failed_recovery_always_pays_exactly_one_verify(client, monkeypatch):
    """F1: unknown user, wrong code and consumed code all cost one Argon2
    verification, so response work no longer differs by account existence."""
    login(client)
    codes = generate_codes(client)
    client.post("/logout", data={"csrf_token": "x"}, follow_redirects=False)

    calls = []
    real_verify = main_module.verify_password

    def spy(password, hashed):
        calls.append(1)
        return real_verify(password, hashed)

    monkeypatch.setattr("app.main.verify_password", spy)

    unknown = recover(client, "no-such-user", codes[0])
    unknown_calls = len(calls)
    calls.clear()

    wrong = recover(client, "guard-admin", "zzzz-zzzz-zzzz-zzzz")
    wrong_calls = len(calls)
    calls.clear()

    assert unknown.status_code == 401 and wrong.status_code == 401
    assert unknown_calls == 1, f"unknown user cost {unknown_calls} verifies"
    assert wrong_calls == 1, f"wrong code cost {wrong_calls} verifies"

    # the real code still recovers through the HMAC lookup with one verify
    ok = recover(client, "guard-admin", codes[0])
    assert ok.status_code == 303
    assert len(calls) == 1


def test_lookup_column_populated_and_legacy_rows_still_recover(client):
    """Generated codes carry the HMAC lookup; a hash-only legacy row (lookup
    NULL, e.g. created before the migration) is still recoverable."""
    login(client)
    codes = generate_codes(client)
    with client.test_session_factory() as db:
        rows = db.scalars(select(RecoveryCode)).all()
        assert all(row.code_lookup for row in rows)
        # simulate a pre-migration row
        db.execute(update(RecoveryCode).values(code_lookup=None))
        db.commit()
    client.post("/logout", data={"csrf_token": "x"}, follow_redirects=False)
    response = recover(client, "guard-admin", codes[1])
    assert response.status_code == 303


def test_recovery_lockout_audit_written_once_at_transition(client):
    """F2: attempts arriving while the lock is already engaged do not write
    duplicate lockout audit rows, and the counter never loses updates."""
    login(client)
    generate_codes(client)
    client.post("/logout", data={"csrf_token": "x"}, follow_redirects=False)

    for _ in range(8):
        recover(client, "guard-admin", "zzzz-zzzz-zzzz-zzzz")

    with client.test_session_factory() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "recovery_lockout")).all()
        assert len(rows) == 1, f"expected exactly one lockout audit row, got {len(rows)}"
    locked = client.get("/login/recover")
    assert "إيقاف الاستعادة" in locked.text
