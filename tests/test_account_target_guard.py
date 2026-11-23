"""Wrong-target guard (bc74a88 follow-up, peer-confirmed id collision).

Account actions bind the numeric id in the URL to the username shown on the
rendered page via a hidden field, revalidated inside the credential write
lock. A stale id (for example after a restore remapped ids) is refused with
error=stale instead of mutating the wrong account.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, Department, EmployeeCodeCounter, LeaveType
from app.security import hash_password, verify_password

ADMIN_PW = "Target-Guard-Admin-123!"
TARGET_PW = "Target-Guard-Target-123!"


@pytest.fixture()
def guard_client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        db.add_all([
            AdminAccount(username="guard-admin", password_hash=hash_password(ADMIN_PW),
                         display_name="مدير الحارس", role="system_admin", active=True),
            AdminAccount(username="guard-target", password_hash=hash_password(TARGET_PW),
                         display_name="الحساب الهدف", role="finance", active=True),
            Department(name="Guard Department", active=True),
            EmployeeCodeCounter(id=1, last_number=0),
            LeaveType(code="ANNUAL", name_ar="سنوية", name_en="Annual", is_paid=True),
        ])
        db.commit()

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", factory)
    try:
        with TestClient(app) as client:
            client.test_session_factory = factory
            yield client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _login(client):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": "guard-admin", "password": ADMIN_PW, "csrf_token": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _target(client):
    with client.test_session_factory() as db:
        return db.scalar(select(AdminAccount).where(AdminAccount.username == "guard-target"))


def test_deactivate_with_stale_username_is_refused(guard_client):
    _login(guard_client)
    target = _target(guard_client)
    page = guard_client.get("/settings/accounts")
    response = guard_client.post(
        f"/settings/accounts/{target.id}/deactivate",
        data={"csrf_token": _csrf(page), "reason": "اختبار", "target_username": "someone-else"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=stale" in response.headers["location"]
    assert _target(guard_client).active is True


def test_deactivate_with_matching_username_works(guard_client):
    _login(guard_client)
    target = _target(guard_client)
    page = guard_client.get("/settings/accounts")
    response = guard_client.post(
        f"/settings/accounts/{target.id}/deactivate",
        data={"csrf_token": _csrf(page), "reason": "اختبار", "target_username": "guard-target"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "saved=deactivated" in response.headers["location"]
    assert _target(guard_client).active is False


def test_reset_password_with_stale_username_is_refused(guard_client):
    _login(guard_client)
    target = _target(guard_client)
    page = guard_client.get("/settings/accounts")
    response = guard_client.post(
        f"/settings/accounts/{target.id}/reset-password",
        data={
            "csrf_token": _csrf(page),
            "reason": "اختبار",
            "current_password": ADMIN_PW,
            "target_username": "someone-else",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=stale" in response.headers["location"]
    assert verify_password(TARGET_PW, _target(guard_client).password_hash)


def test_activate_with_stale_username_is_refused(guard_client):
    _login(guard_client)
    page = guard_client.get("/settings/accounts")
    target = _target(guard_client)
    guard_client.post(
        f"/settings/accounts/{target.id}/deactivate",
        data={"csrf_token": _csrf(page), "reason": "اختبار", "target_username": "guard-target"},
        follow_redirects=False,
    )
    assert _target(guard_client).active is False
    page = guard_client.get("/settings/accounts")
    response = guard_client.post(
        f"/settings/accounts/{target.id}/activate",
        data={"csrf_token": _csrf(page), "reason": "اختبار", "target_username": "someone-else"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=stale" in response.headers["location"]
    assert _target(guard_client).active is False
