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
    login_key,
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


def test_three_failures_lock_login_for_one_hour(factory):
    with factory() as db:
        for _ in range(FAILED_ATTEMPTS_LIMIT - 1):
            assert record_failed_attempt(db, "lock-admin", actor="test") is False
        assert record_failed_attempt(db, "lock-admin", actor="test") is True
        remaining = lockout_remaining(db, "lock-admin")
        assert remaining is not None
        assert timedelta(minutes=59) < remaining <= LOCKOUT_DURATION
        assert FAILED_ATTEMPTS_LIMIT == 3 and LOCKOUT_DURATION == timedelta(hours=1)
        lock_event = db.scalar(select(AuditLog).where(AuditLog.action == "login_lockout"))
        assert lock_event is not None


def test_successful_login_clears_counter(factory):
    with factory() as db:
        record_failed_attempt(db, "lock-admin", actor="test")
        record_failed_attempt(db, "lock-admin", actor="test")
        clear_attempts(db, "lock-admin", actor="test")
        setting = db.get(SystemSetting, login_key("lock-admin"))
        assert setting is not None
        assert "0" in setting.value
        assert lockout_remaining(db, "lock-admin") is None


def test_expired_lockout_allows_login(factory):
    with factory() as db:
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        for _ in range(FAILED_ATTEMPTS_LIMIT):
            record_failed_attempt(db, "lock-admin", actor="test", now=past)
        assert lockout_remaining(db, "lock-admin") is None


def test_route_locks_after_reaching_failure_limit_and_blocks_even_correct_password(client):
    for attempt in range(FAILED_ATTEMPTS_LIMIT - 1):
        response = post_login(client, WRONG)
        assert response.status_code == 401, f"attempt {attempt + 1} should be 401"
    final_attempt = post_login(client, WRONG)
    assert final_attempt.status_code == 429
    assert "تم إيقاف الدخول مؤقتًا" in final_attempt.text
    assert "60" in final_attempt.text
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
        clear_attempts(db, "lock-admin", actor="test")
    assert post_login(client, PASSWORD).status_code == 303

    events = [payload["event"] for payload in sent]
    assert "login_lockout" in events
    assert "login_success" in events
    lockout_payload = next(p for p in sent if p["event"] == "login_lockout")
    assert lockout_payload["failed_attempts"] == FAILED_ATTEMPTS_LIMIT
    assert lockout_payload["locked_minutes"] == 60


def test_alert_failure_does_not_break_login(client, monkeypatch):
    def broken_urlopen(request, timeout):
        raise OSError("network down")

    monkeypatch.setattr("app.login_security.settings.login_alert_webhook_url", "https://alerts.example.test/hook", raising=False)
    monkeypatch.setattr("app.login_security.urllib.request.urlopen", broken_urlopen)
    assert post_login(client, PASSWORD).status_code == 303


def _post_as(client, username, password):
    page = client.get("/login")
    return client.post("/login", data={"username": username, "password": password, "csrf_token": csrf(page)},
                       follow_redirects=False)


def test_lock_is_per_account_not_global(client, factory):
    with factory() as db:
        db.add(AdminAccount(username="other-user", password_hash=hash_password(PASSWORD), active=True))
        db.commit()
    for _ in range(FAILED_ATTEMPTS_LIMIT):
        post_login(client, WRONG)
    assert post_login(client, PASSWORD).status_code == 429          # lock-admin is locked
    assert _post_as(client, "other-user", PASSWORD).status_code == 303   # another account is not


def test_failures_on_two_names_do_not_add_up(client, factory):
    with factory() as db:
        db.add(AdminAccount(username="other-user", password_hash=hash_password(PASSWORD), active=True))
        db.commit()
    for _ in range(FAILED_ATTEMPTS_LIMIT - 1):
        assert post_login(client, WRONG).status_code == 401
        assert _post_as(client, "other-user", WRONG).status_code == 401
    assert post_login(client, PASSWORD).status_code == 303


def test_name_case_and_padding_share_one_counter(factory):
    assert login_key("Lock-Admin ") == login_key("lock-admin")
    with factory() as db:
        record_failed_attempt(db, "LOCK-ADMIN", actor="t")
        record_failed_attempt(db, " lock-admin", actor="t")
        assert record_failed_attempt(db, "Lock-Admin", actor="t") is True
        assert lockout_remaining(db, "lock-admin") is not None


def test_known_and_unknown_names_get_identical_responses(client):
    seen = {}
    for name in ("lock-admin", "no-such-user"):
        seen[name] = [(_post_as(client, name, WRONG).status_code) for _ in range(FAILED_ATTEMPTS_LIMIT + 1)]
    assert seen["lock-admin"] == seen["no-such-user"] == [401, 401, 429, 429]
    texts = set()
    for name in ("lock-admin", "no-such-user"):
        texts.add(_post_as(client, name, PASSWORD + "x").text.replace(name, "N"))
    assert len(texts) == 1


def test_counter_table_stays_bounded_after_many_random_names(factory, monkeypatch):
    import app.login_security as ls
    from app.models import SystemSetting
    monkeypatch.setattr(ls, "MAX_COUNTER_ROWS", 50)
    t0 = datetime.now(timezone.utc)
    with factory() as db:
        for n in range(500):
            record_failed_attempt(db, f"guess-{n}", actor="t", now=t0 + timedelta(seconds=n))
        count = db.query(SystemSetting).filter(SystemSetting.key.like("login_security:%")).count()
        assert count <= 50
        # an actively locked name is never evicted by the cap
        for _ in range(FAILED_ATTEMPTS_LIMIT):
            record_failed_attempt(db, "locked-one", actor="t", now=t0 + timedelta(seconds=600))
        for n in range(500, 700):
            record_failed_attempt(db, f"guess-{n}", actor="t", now=t0 + timedelta(seconds=700 + n))
        assert lockout_remaining(db, "locked-one", now=t0 + timedelta(seconds=1000)) is not None
        assert db.query(SystemSetting).filter(SystemSetting.key.like("login_security:%")).count() <= 51


def test_idle_rows_are_purged_after_the_lock_window(factory):
    from app.models import SystemSetting
    t0 = datetime.now(timezone.utc)
    with factory() as db:
        record_failed_attempt(db, "old-guess", actor="t", now=t0)
        record_failed_attempt(db, "new-guess", actor="t", now=t0 + timedelta(minutes=61))
        keys = {r.key for r in db.query(SystemSetting).filter(SystemSetting.key.like("login_security:%"))}
        assert keys == {ls_key("new-guess")}


def ls_key(name):
    from app.login_security import login_key
    return login_key(name)


def test_expired_lock_gives_three_fresh_attempts(factory):
    with factory() as db:
        t0 = datetime.now(timezone.utc)
        for _ in range(FAILED_ATTEMPTS_LIMIT):
            record_failed_attempt(db, "lock-admin", actor="t", now=t0)
        later = t0 + timedelta(minutes=61)
        assert lockout_remaining(db, "lock-admin", now=later) is None
        assert record_failed_attempt(db, "lock-admin", actor="t", now=later) is False   # 1st fresh failure, not the 4th
        assert record_failed_attempt(db, "lock-admin", actor="t", now=later) is False
        assert record_failed_attempt(db, "lock-admin", actor="t", now=later) is True    # 3rd fresh failure locks again


def test_lock_expires_after_one_hour_only(factory):
    with factory() as db:
        t0 = datetime.now(timezone.utc)
        for _ in range(FAILED_ATTEMPTS_LIMIT):
            record_failed_attempt(db, "lock-admin", actor="t", now=t0)
        assert lockout_remaining(db, "lock-admin", now=t0 + timedelta(minutes=59)) is not None
        assert lockout_remaining(db, "lock-admin", now=t0 + timedelta(minutes=61)) is None


def test_old_global_counter_row_is_ignored(factory):
    with factory() as db:
        db.add(SystemSetting(key="login_security", value='{"failed_attempts": 9, "locked_until": "2999-01-01T00:00:00+00:00"}',
                             updated_by="old"))
        db.commit()
        assert lockout_remaining(db, "lock-admin") is None


def test_login_page_shows_no_global_lock_banner(client, factory):
    with factory() as db:
        for _ in range(FAILED_ATTEMPTS_LIMIT):
            record_failed_attempt(db, "lock-admin", actor="t")
    assert "تم إيقاف الدخول" not in client.get("/login").text


def test_unknown_name_flood_writes_no_audit_and_sends_no_alerts_but_answers_identically(client, factory, monkeypatch):
    sent = []
    monkeypatch.setattr("app.main.send_login_alert", lambda *a, **k: sent.append(("webhook", a)))
    monkeypatch.setattr("app.main.send_telegram_login_event", lambda *a, **k: sent.append(("telegram", a)))
    unknown = [_post_as(client, "ghost-0", WRONG) for _ in range(FAILED_ATTEMPTS_LIMIT)]
    for n in range(30):
        _post_as(client, f"ghost-flood-{n}", WRONG)
    assert sent == []
    with factory() as db:
        assert db.query(AuditLog).filter(AuditLog.action == "login_lockout").count() == 0
    real = [_post_as(client, "lock-admin", WRONG) for _ in range(FAILED_ATTEMPTS_LIMIT)]
    # same status sequence for a known name as for an unknown name (401, 401, 429)
    assert [r.status_code for r in real] == [r.status_code for r in unknown] == [401, 401, 429]
    assert real[-1].text.replace("lock-admin", "N") == unknown[2].text.replace("ghost-0", "N")
    assert {s[0] for s in sent} == {"webhook", "telegram"}
    with factory() as db:
        assert db.query(AuditLog).filter(AuditLog.action == "login_lockout").count() == 1
