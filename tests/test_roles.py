"""Named account roles: system admin / finance / assistant manager access matrix."""
import re
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    AdminAccount,
    AuditLog,
    Department,
    Employee,
    EmployeeCodeCounter,
    EmployeeServicePeriod,
    LeaveType,
)
from app.security import hash_password

ADMIN_PW = "Guard-Test-Password-123!"
FINANCE_PW = "Finance-Test-Password-123!"
ASSISTANT_PW = "Assistant-Test-Password-123!"


@pytest.fixture()
def role_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        db.add_all([
            AdminAccount(
                username="guard-admin",
                password_hash=hash_password(ADMIN_PW),
                display_name="مدير النظام التجريبي",
                role="system_admin",
                active=True,
            ),
            Department(name="Guard Test Department", active=True),
            EmployeeCodeCounter(id=1, last_number=0),
            LeaveType(code="ANNUAL", name_ar="سنوية", name_en="Annual", is_paid=True),
        ])
        db.flush()
        employee = Employee(
            code="MY-TEST-ROL-0001",
            full_name="Synthetic Role Employee",
            department_id=1,
            position="Test Position",
            hire_date=date(2026, 1, 1),
            status="active",
            basic_salary=Decimal("6000.00"),
            daily_hours=Decimal("8"),
        )
        db.add(employee)
        db.flush()
        db.add(EmployeeServicePeriod(
            employee_id=employee.id,
            start_date=date(2026, 1, 1),
            end_date=None,
            start_reason="Synthetic service period",
        ))
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
    assert response.status_code == 303
    return response


def create_account(client, username, password, role, name="شخص تجريبي"):
    page = client.get("/settings/accounts")
    response = client.post(
        "/settings/accounts",
        data={
            "csrf_token": csrf(page),
            "display_name": name,
            "username": username,
            "role": role,
            "password": password,
            "confirm_password": password,
            "reason": "إنشاء حساب تجريبي",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error" not in response.headers["location"]


@pytest.fixture()
def three_clients(role_client):
    """Admin creates finance + assistant accounts; returns three logged-in clients."""
    admin = role_client
    login(admin, "guard-admin", ADMIN_PW)
    create_account(admin, "finance-user", FINANCE_PW, "finance", "محاسب تجريبي")
    create_account(admin, "assistant-user", ASSISTANT_PW, "assistant_manager", "مساعد تجريبي")
    base_url = str(admin.base_url)
    finance = TestClient(app, base_url=base_url)
    assistant = TestClient(app, base_url=base_url)
    finance.test_session_factory = admin.test_session_factory
    assistant.test_session_factory = admin.test_session_factory
    login(finance, "finance-user", FINANCE_PW)
    login(assistant, "assistant-user", ASSISTANT_PW)
    return admin, finance, assistant


def test_admin_creates_named_accounts_with_audit(role_client):
    client = role_client
    login(client, "guard-admin", ADMIN_PW)
    create_account(client, "finance-user", FINANCE_PW, "finance", "محاسب تجريبي")
    with client.test_session_factory() as db:
        account = db.scalar(select(AdminAccount).where(AdminAccount.username == "finance-user"))
        assert account.role == "finance"
        assert account.display_name == "محاسب تجريبي"
        audits = db.scalars(select(AuditLog).where(AuditLog.action == "account_created")).all()
        assert len(audits) == 1
        assert audits[0].actor == "guard-admin"
        assert audits[0].reason == "إنشاء حساب تجريبي"


def test_admin_cannot_create_second_system_admin(role_client):
    client = role_client
    login(client, "guard-admin", ADMIN_PW)
    page = client.get("/settings/accounts")
    response = client.post(
        "/settings/accounts",
        data={
            "csrf_token": csrf(page),
            "display_name": "مدير ثانٍ", "username": "admin2", "role": "system_admin",
            "password": FINANCE_PW, "confirm_password": FINANCE_PW, "reason": "محاولة",
        },
        follow_redirects=False,
    )
    assert "error=role" in response.headers["location"]


def test_assistant_has_no_payroll_access_anywhere(three_clients):
    _, _, assistant = three_clients
    for url in ("/payroll", "/advances", "/staff-actions", "/employees", "/", "/ai", "/settings/backup", "/settings/audit", "/settings/accounts"):
        response = assistant.get(url, follow_redirects=False)
        assert response.status_code in (303, 403), (url, response.status_code)
        if response.status_code == 303:
            assert url == "/"  # assistant home redirects to attendance
    # payroll POST is refused server-side even with a valid session
    page = assistant.get("/attendance/review")
    response = assistant.post(
        "/payroll",
        data={"csrf_token": csrf(page) if "csrf_token" in page.text else "", "year": 2026, "month": 10},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_assistant_attendance_write_and_reports_scope(three_clients):
    _, _, assistant = three_clients
    assert assistant.get("/attendance/review").status_code == 200
    assert assistant.get("/attendance/import").status_code == 200
    assert assistant.get("/leave").status_code == 200
    assert assistant.get("/security-roster").status_code == 200
    # attendance report allowed
    response = assistant.get("/reports?report_type=attendance&period=this_month")
    assert response.status_code == 200
    # payroll report refused
    response = assistant.get("/reports?report_type=payroll&period=this_month", follow_redirects=False)
    assert response.status_code == 403
    # payroll type smuggled into an attendance selection is refused
    response = assistant.get(
        "/reports?report_type=attendance&period=this_month&fields=pay_basic",
        follow_redirects=False,
    )
    assert response.status_code == 403
    # the all-modules workbook export is refused
    response = assistant.get("/reports/export.xlsx?start_date=2026-10-01&end_date=2026-10-31", follow_redirects=False)
    assert response.status_code == 403


def test_finance_payroll_full_attendance_readonly(three_clients):
    _, finance, _ = three_clients
    assert finance.get("/payroll").status_code == 200
    assert finance.get("/advances").status_code == 200
    assert finance.get("/staff-actions").status_code == 200
    assert finance.get("/employees").status_code == 200
    assert finance.get("/").status_code == 200
    # attendance visible but read-only
    assert finance.get("/attendance/review").status_code == 200
    page = finance.get("/attendance/review")
    token = csrf(page) if "csrf_token" in page.text else ""
    response = finance.post(
        "/attendance/manual",
        data={"csrf_token": token, "employee_id": "1", "work_date": "2026-10-05",
              "clock_in": "2026-10-05T09:00", "clock_out": "2026-10-05T17:00", "reason": "سبب تجريبي"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    # no system settings, no AI, no leave
    for url in ("/settings/backup", "/settings/audit", "/settings/accounts", "/ai", "/leave"):
        assert finance.get(url, follow_redirects=False).status_code == 403, url
    # employee edits refused
    response = finance.post(
        "/employees/1/edit",
        data={"csrf_token": token, "full_name": "x", "position": "x", "reason": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_non_admin_cannot_manage_accounts(three_clients):
    _, finance, assistant = three_clients
    assert finance.get("/settings/accounts", follow_redirects=False).status_code == 403
    assert assistant.get("/settings/accounts", follow_redirects=False).status_code == 403


def test_deactivate_closes_sessions_and_last_admin_protected(three_clients):
    admin, finance, _ = three_clients
    with admin.test_session_factory() as db:
        finance_id = db.scalar(select(AdminAccount).where(AdminAccount.username == "finance-user")).id
    page = admin.get("/settings/accounts")
    response = admin.post(
        f"/settings/accounts/{finance_id}/deactivate",
        data={"csrf_token": csrf(page), "reason": "انتهاء مهمة"},
        follow_redirects=False,
    )
    assert "error" not in response.headers["location"]
    # the deactivated account's live session is dead
    assert finance.get("/payroll", follow_redirects=False).status_code in (303, 401)
    # and it cannot sign in again
    login_page = finance.get("/login")
    response = finance.post(
        "/login",
        data={"username": "finance-user", "password": FINANCE_PW, "csrf_token": csrf(login_page)},
        follow_redirects=False,
    )
    assert response.status_code == 401
    # the sole system admin cannot be deactivated
    page = admin.get("/settings/accounts")
    response = admin.post(
        "/settings/accounts/1/deactivate",
        data={"csrf_token": csrf(page), "reason": "اختبار"},
        follow_redirects=False,
    )
    assert "error" in response.headers["location"]


def test_each_account_changes_own_password(three_clients):
    _, finance, _ = three_clients
    page = finance.get("/settings/password")
    assert page.status_code == 200
    response = finance.post(
        "/settings/password",
        data={
            "csrf_token": csrf(page),
            "current_password": FINANCE_PW,
            "new_password": "Finance-New-Password-456!",
            "confirm_password": "Finance-New-Password-456!",
        },
    )
    assert response.status_code == 200
    # old password no longer works
    fresh = TestClient(app, base_url=str(finance.base_url))
    login_page = fresh.get("/login")
    response = fresh.post(
        "/login",
        data={"username": "finance-user", "password": FINANCE_PW, "csrf_token": csrf(login_page)},
        follow_redirects=False,
    )
    assert response.status_code == 401
