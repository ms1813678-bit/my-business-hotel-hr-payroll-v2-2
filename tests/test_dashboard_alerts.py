"""Dashboard operational alerts: backup state and missing fingerprint IDs."""
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, BackupRun, Department, Employee, SystemSetting
from app.security import hash_password

PASSWORD = "Alerts-Test-Password-123!"


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with session_factory() as db:
        db.add(AdminAccount(username="alerts-admin", password_hash=hash_password(PASSWORD), active=True))
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
        data={"username": "alerts-admin", "password": PASSWORD, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def set_backup_root(client):
    with client.test_session_factory() as db:
        db.add(SystemSetting(key="backup_root", value="/tmp/backups", updated_by="test"))
        db.commit()


def add_employee(client, fingerprint=None, code="MY-0001"):
    with client.test_session_factory() as db:
        department = Department(name=f"قسم {code}", active=True)
        db.add(department)
        db.flush()
        db.add(Employee(
            code=code, full_name="موظف تجريبي", department_id=department.id,
            position="موظف استقبال", hire_date=date(2026, 1, 1),
            basic_salary=Decimal("6000.00"), daily_hours=Decimal("8.00"),
            fingerprint_id=fingerprint, status="active",
        ))
        db.commit()


def test_missing_backup_root_alert(client):
    login(client)
    page = client.get("/")
    assert "لم يتم ضبط موقع النسخ الاحتياطي على قرص خارجي بعد." in page.text


def test_no_successful_backup_alert(client):
    login(client)
    set_backup_root(client)
    page = client.get("/")
    assert "لا توجد نسخة احتياطية ناجحة بعد." in page.text
    assert "لم يتم ضبط موقع النسخ الاحتياطي" not in page.text


def test_stale_backup_alert_with_days(client):
    login(client)
    set_backup_root(client)
    with client.test_session_factory() as db:
        db.add(BackupRun(
            backup_type="manual", status="success", initiated_by="test",
            created_at=datetime.now(timezone.utc) - timedelta(days=9),
        ))
        db.commit()
    page = client.get("/")
    assert "آخر نسخة احتياطية ناجحة منذ" in page.text
    assert "9" in page.text


def test_recent_backup_shows_no_stale_alert(client):
    login(client)
    set_backup_root(client)
    with client.test_session_factory() as db:
        db.add(BackupRun(backup_type="manual", status="success", initiated_by="test"))
        db.commit()
    page = client.get("/")
    assert "آخر نسخة احتياطية ناجحة منذ" not in page.text
    assert "يحتاج انتباهك" not in page.text


def test_failed_backup_alert(client):
    login(client)
    set_backup_root(client)
    with client.test_session_factory() as db:
        db.add(BackupRun(
            backup_type="manual", status="failed", initiated_by="test",
            error_message="disk full",
        ))
        db.commit()
    page = client.get("/")
    assert "فشلت آخر محاولة نسخ احتياطي" in page.text


def test_missing_fingerprint_alert(client):
    login(client)
    set_backup_root(client)
    with client.test_session_factory() as db:
        db.add(BackupRun(backup_type="manual", status="success", initiated_by="test"))
        db.commit()
    add_employee(client, fingerprint=None)
    page = client.get("/")
    assert "موظفون نشطون بدون رقم بصمة" in page.text
    add_employee(client, fingerprint=None, code="MY-0002")
    page = client.get("/")
    assert "موظفون نشطون بدون رقم بصمة: 2" in page.text


def test_employee_with_fingerprint_clears_alert(client):
    login(client)
    set_backup_root(client)
    with client.test_session_factory() as db:
        db.add(BackupRun(backup_type="manual", status="success", initiated_by="test"))
        db.commit()
    add_employee(client, fingerprint="FP-100")
    page = client.get("/")
    assert "موظفون نشطون بدون رقم بصمة" not in page.text
    assert "يحتاج انتباهك" not in page.text
