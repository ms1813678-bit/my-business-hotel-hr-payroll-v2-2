"""Audit log viewer: access control, listing, filters, pagination."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, AuditLog
from app.security import hash_password

PASSWORD = "Audit-Test-Password-123!"


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with session_factory() as db:
        db.add(AdminAccount(username="audit-admin", password_hash=hash_password(PASSWORD), active=True))
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


def login(client):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": "audit-admin", "password": PASSWORD, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def seed_entries(client, count, action="payroll_run_approved", entity_type="payroll_run"):
    with client.test_session_factory() as db:
        base = datetime.now(timezone.utc) - timedelta(hours=1)
        for index in range(count):
            db.add(AuditLog(
                actor="audit-admin",
                action=action,
                entity_type=entity_type,
                entity_id=f"RUN-{index:03d}",
                reason=f"سبب رقم {index}",
                created_at=base + timedelta(minutes=index),
            ))
        db.commit()


def test_audit_page_requires_admin(client):
    assert client.get("/settings/audit").status_code == 401
    login(client)
    assert client.get("/settings/audit").status_code == 200


def test_audit_page_lists_entries_with_arabic_labels(client):
    login(client)
    seed_entries(client, 3)
    page = client.get("/settings/audit")
    assert page.status_code == 200
    assert "اعتماد مسير رواتب" in page.text
    assert "مسير رواتب" in page.text
    assert "RUN-002" in page.text
    assert "سبب رقم 1" in page.text


def test_audit_search_and_entity_filter(client):
    login(client)
    seed_entries(client, 2, action="employee_created", entity_type="employee")
    seed_entries(client, 2, action="payroll_run_approved", entity_type="payroll_run")
    page = client.get("/settings/audit", params={"entity_type": "employee"})
    assert "إضافة موظف" in page.text
    assert "اعتماد مسير رواتب" not in page.text
    page = client.get("/settings/audit", params={"q": "RUN-001"})
    assert "RUN-001" in page.text
    assert "RUN-000" not in page.text


def test_audit_date_filter(client):
    login(client)
    with client.test_session_factory() as db:
        db.add(AuditLog(
            actor="audit-admin", action="department_created", entity_type="department",
            entity_id="OLD-1", created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        ))
        db.add(AuditLog(
            actor="audit-admin", action="department_updated", entity_type="department",
            entity_id="NEW-1", created_at=datetime.now(timezone.utc),
        ))
        db.commit()
    page = client.get("/settings/audit", params={"from_date": "2026-06-01"})
    assert "NEW-1" in page.text
    assert "OLD-1" not in page.text
    page = client.get("/settings/audit", params={"to_date": "2026-02-01"})
    assert "OLD-1" in page.text
    assert "NEW-1" not in page.text


def test_audit_pagination(client):
    login(client)
    seed_entries(client, 120)
    page_one = client.get("/settings/audit")
    assert "RUN-119" in page_one.text
    assert "RUN-070" in page_one.text
    assert "RUN-069" not in page_one.text
    page_three = client.get("/settings/audit", params={"page": 3})
    assert "RUN-019" in page_three.text
    assert "RUN-000" in page_three.text
