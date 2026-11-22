"""Admin password change from the UI: validation, session rotation, audit."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, AuditLog
from app.security import hash_password, verify_password

PASSWORD = "Current-Test-Password-123!"
NEW_PASSWORD = "Brand-New-Test-Password-456!"


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with session_factory() as db:
        db.add(AdminAccount(username="pw-admin", password_hash=hash_password(PASSWORD), active=True))
        db.commit()

    def override_get_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", session_factory)
    try:
        with TestClient(app) as test_client:
            test_client.test_session_factory = session_factory
            yield test_client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def login(client, password=PASSWORD):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": "pw-admin", "password": password, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def change(client, current, new, confirm):
    page = client.get("/settings/password")
    return client.post(
        "/settings/password",
        data={
            "current_password": current,
            "new_password": new,
            "confirm_password": confirm,
            "csrf_token": csrf(page),
        },
    )


def test_password_page_requires_admin_and_csrf(client):
    assert client.get("/settings/password").status_code == 401
    login(client)
    assert client.get("/settings/password").status_code == 200
    assert client.post("/settings/password", data={}).status_code == 403


def test_wrong_current_password_rejected(client):
    login(client)
    response = change(client, "Wrong-Current-Password-999!", NEW_PASSWORD, NEW_PASSWORD)
    assert response.status_code == 400
    assert "كلمة المرور الحالية غير صحيحة" in response.text


def test_mismatched_confirmation_rejected(client):
    login(client)
    response = change(client, PASSWORD, NEW_PASSWORD, "Different-Confirm-Password-789!")
    assert response.status_code == 400
    assert "غير متطابقين" in response.text


def test_short_password_rejected(client):
    login(client)
    response = change(client, PASSWORD, "short", "short")
    assert response.status_code == 400
    assert "12" in response.text


def test_same_password_rejected(client):
    login(client)
    response = change(client, PASSWORD, PASSWORD, PASSWORD)
    assert response.status_code == 400
    assert "تختلف عن الحالية" in response.text


def test_successful_change_rotates_sessions_and_audits(client):
    login(client)
    response = change(client, PASSWORD, NEW_PASSWORD, NEW_PASSWORD)
    assert response.status_code == 200
    assert "تم تغيير كلمة المرور بنجاح" in response.text

    # Current session stays signed in with the bumped session version.
    assert client.get("/").status_code == 200

    with client.test_session_factory() as db:
        admin = db.scalar(select(AdminAccount).where(AdminAccount.username == "pw-admin"))
        assert verify_password(NEW_PASSWORD, admin.password_hash)
        assert not verify_password(PASSWORD, admin.password_hash)
        assert admin.session_version == 1
        entry = db.scalar(select(AuditLog).where(AuditLog.action == "admin_password_changed"))
        assert entry is not None
        assert entry.actor == "pw-admin"

    # Old password no longer signs in; the new one does.
    fresh = TestClient(app)
    page = fresh.get("/login")
    old_login = fresh.post(
        "/login",
        data={"username": "pw-admin", "password": PASSWORD, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert old_login.status_code == 401
    page = fresh.get("/login")
    new_login = fresh.post(
        "/login",
        data={"username": "pw-admin", "password": NEW_PASSWORD, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert new_login.status_code == 303
