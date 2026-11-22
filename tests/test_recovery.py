"""Sole-sysadmin one-use recovery codes: generation, consumption, throttling."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, AuditLog, RecoveryCode
from app.security import hash_password

ADMIN_PW = "Guard-Test-Password-123!"
NEW_PW = "Recovered-New-Password-77!"


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


def login(client, username, password):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response


def generate_codes(client, password=ADMIN_PW):
    page = client.get("/settings/accounts")
    return client.post(
        "/settings/accounts/recovery-codes",
        data={"csrf_token": csrf(page), "current_password": password},
        follow_redirects=False,
    )


def extract_codes(response):
    return re.findall(r"<code>([^<]+)</code>", response.text)


def recover(client, username, code, new_password=NEW_PW, confirm=None):
    page = client.get("/login/recover")
    return client.post(
        "/login/recover",
        data={
            "csrf_token": csrf(page),
            "username": username,
            "recovery_code": code,
            "new_password": new_password,
            "confirm_password": confirm if confirm is not None else new_password,
        },
        follow_redirects=False,
    )


def test_generation_requires_admin_password_and_codes_are_hashed(client):
    login(client, "guard-admin", ADMIN_PW)
    wrong = generate_codes(client, password="not-the-admin-password")
    assert wrong.status_code == 303
    assert wrong.headers["location"] == "/settings/accounts?error=auth"

    response = generate_codes(client)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    codes = extract_codes(response)
    assert len(codes) == 10
    assert len(set(codes)) == 10, "codes are unique"

    with client.test_session_factory() as db:
        stored = db.scalars(select(RecoveryCode)).all()
        assert len(stored) == 10
        for row in stored:
            assert row.used_at is None
            assert row.code_hash not in codes, "only hashes are stored"
        audit = db.scalar(select(AuditLog).where(
            AuditLog.action == "recovery_codes_generated"))
        assert audit is not None and audit.actor == "guard-admin"
        assert all(code not in (audit.new_value or "") for code in codes)


def test_regeneration_invalidates_previous_codes(client):
    login(client, "guard-admin", ADMIN_PW)
    old_codes = extract_codes(generate_codes(client))
    new_codes = extract_codes(generate_codes(client))
    assert set(old_codes) != set(new_codes)
    with client.test_session_factory() as db:
        assert db.scalars(select(RecoveryCode)).all().__len__() == 10


def test_recovery_sets_new_password_and_code_is_single_use(client):
    login(client, "guard-admin", ADMIN_PW)
    codes = extract_codes(generate_codes(client))
    client.post("/logout", data={"csrf_token": "x"}, follow_redirects=False)

    response = recover(client, "guard-admin", codes[0])
    assert response.status_code == 303
    assert response.headers["location"] == "/login?recovered=1"

    with client.test_session_factory() as db:
        account = db.scalar(select(AdminAccount))
        assert account.must_change_password is False
        audit = db.scalar(select(AuditLog).where(
            AuditLog.action == "admin_password_recovered"))
        assert audit is not None
        assert codes[0] not in (audit.new_value or "")
        used = db.scalars(select(RecoveryCode).where(RecoveryCode.used_at.isnot(None))).all()
        assert len(used) == 1

    # the old password no longer works; the new one does
    old = client.post(
        "/login",
        data={"username": "guard-admin", "password": ADMIN_PW,
              "csrf_token": csrf(client.get("/login"))},
        follow_redirects=False,
    )
    assert old.status_code == 401
    login(client, "guard-admin", NEW_PW)

    # the consumed code can never be used again
    client.post("/logout", data={"csrf_token": "x"}, follow_redirects=False)
    again = recover(client, "guard-admin", codes[0], new_password="Another-New-Password-88!")
    assert again.status_code == 401
    assert "بيانات الاستعادة غير صحيحة" in again.text


def test_recovery_failures_are_generic_and_do_not_leak(client):
    login(client, "guard-admin", ADMIN_PW)
    codes = extract_codes(generate_codes(client))
    client.post("/logout", data={"csrf_token": "x"}, follow_redirects=False)

    bad_user = recover(client, "no-such-user", codes[0])
    bad_code = recover(client, "guard-admin", "zzzz-zzzz-zzzz-zzzz")
    assert bad_user.status_code == 401 and bad_code.status_code == 401
    assert "بيانات الاستعادة غير صحيحة" in bad_user.text
    assert bad_user.text.count("بيانات الاستعادة غير صحيحة") == bad_code.text.count(
        "بيانات الاستعادة غير صحيحة")

    mismatch = recover(client, "guard-admin", codes[0], confirm="different-password-99")
    assert mismatch.status_code == 400


def test_recovery_has_own_throttle_independent_of_login_lockout(client):
    login(client, "guard-admin", ADMIN_PW)
    extract_codes(generate_codes(client))
    client.post("/logout", data={"csrf_token": "x"}, follow_redirects=False)

    # engage the login lockout (2 failed logins); recovery must stay open
    for _ in range(2):
        client.post(
            "/login",
            data={"username": "guard-admin", "password": "wrong",
                  "csrf_token": csrf(client.get("/login"))},
        )
    locked_login = client.get("/login")
    assert "إيقاف الدخول" in locked_login.text
    recover_page = client.get("/login/recover")
    assert recover_page.status_code == 200
    assert "إيقاف الاستعادة" not in recover_page.text

    # recovery's own throttle engages after 5 bad attempts
    for _ in range(5):
        recover(client, "guard-admin", "zzzz-zzzz-zzzz-zzzz")
    locked = client.get("/login/recover")
    assert "إيقاف الاستعادة" in locked.text
    sixth = recover(client, "guard-admin", "zzzz-zzzz-zzzz-zzzz")
    assert sixth.status_code == 429
