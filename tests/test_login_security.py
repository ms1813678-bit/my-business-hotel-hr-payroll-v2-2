"""Login lockout after repeated failures and login event alerts."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.login_security import (
    FAILED_ATTEMPTS_LIMIT,
    LOCKOUT_DURATION,
    clear_attempts,
    lockout_remaining,
    record_failed_attempt,
)
from app.main import app
from app.models import AdminAccount, AuditLog, SystemSetting
from app.security import hash_password

PASSWORD = "Lockout-Test-Password-123!"
WRONG = "Wrong-Lockout-Password-456!"


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with session_factory() as db:
        db.add(AdminAccount(username="lock-admin", password_hash=hash_password(PASSWORD), active=True))
        db.commit()
    def override_get_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", session_factory)
    try:
        yield session_factory
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def client(factory):
    with TestClient(app) as test_client:
        test_client.test_session_factory = factory
        yield test_client


def csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def post_login(client, password):
    page = client.get("/login")
    return client.post(
        "/login",
        data={"username": "lock-admin", "password": password, "csrf_token": csrf(page)},
        follow_redirects=False,
    )


def test_five_failures_lock_login_for_two_hours(factory):
    with factory() as db:
        for _ in range(FAILED_ATTEMPTS_LIMIT - 1):
            assert record_failed_attempt(db, actor="test") is False
        assert record_failed_attempt(db, actor="test") is True
        remaining = lockout_remaining(db)
        assert remaining is not None
        assert timedelta(hours=1, minutes=59) < remaining <= LOCKOUT_DURATION
        lock_event = db.scalar(select(AuditLog).where(AuditLog.action == "login_lockout"))
        assert lock_event is not None


def test_successful_login_clears_counter(factory):
    with factory() as db:
        record_failed_attempt(db, actor="test")
        record_failed_attempt(db, actor="test")
        clear_attempts(db, actor="test")
        setting = db.get(SystemSetting, "login_security")
        assert setting is not None
        assert "0" in setting.value
        assert lockout_remaining(db) is None


def test_expired_lockout_allows_login(factory):
    with factory() as db:
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        for _ in range(FAILED_ATTEMPTS_LIMIT):
            record_failed_attempt(db, actor="test", now=past)
        assert lockout_remaining(db) is None


def test_route_locks_after_reaching_failure_limit_and_blocks_even_correct_password(client):
    for attempt in range(FAILED_ATTEMPTS_LIMIT - 1):
        response = post_login(client, WRONG)
        assert response.status_code == 401, f"attempt {attempt + 1} should be 401"
    final_attempt = post_login(client, WRONG)
    assert final_attempt.status_code == 429
    assert "تم إيقاف الدخول مؤقتًا" in final_attempt.text
    assert "120" in final_attempt.text
    blocked = post_login(client, PASSWORD)
    assert blocked.status_code == 429


def test_route_success_clears_prior_failures(client):
    for _ in range(FAILED_ATTEMPTS_LIMIT - 1):
        assert post_login(client, WRONG).status_code == 401
    assert post_login(client, PASSWORD).status_code == 303
    assert post_login(client, WRONG).status_code == 401


def test_lockout_and_success_emit_webhook_alerts(client, monkeypatch):
    sent = []

    class FakeResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        import json
        sent.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("app.login_security.settings.login_alert_webhook_url", "https://alerts.example.test/hook", raising=False)
    monkeypatch.setattr("app.login_security.urllib.request.urlopen", fake_urlopen)

    for _ in range(FAILED_ATTEMPTS_LIMIT):
        post_login(client, WRONG)
    assert post_login(client, PASSWORD).status_code == 429
    with client.test_session_factory() as db:
        clear_attempts(db, actor="test")
    assert post_login(client, PASSWORD).status_code == 303

    events = [payload["event"] for payload in sent]
    assert "login_lockout" in events
    assert "login_success" in events
    lockout_payload = next(p for p in sent if p["event"] == "login_lockout")
    assert lockout_payload["failed_attempts"] == FAILED_ATTEMPTS_LIMIT
    assert lockout_payload["locked_minutes"] == 120


def test_alert_failure_does_not_break_login(client, monkeypatch):
    def broken_urlopen(request, timeout):
        raise OSError("network down")

    monkeypatch.setattr("app.login_security.settings.login_alert_webhook_url", "https://alerts.example.test/hook", raising=False)
    monkeypatch.setattr("app.login_security.urllib.request.urlopen", broken_urlopen)
    assert post_login(client, PASSWORD).status_code == 303
