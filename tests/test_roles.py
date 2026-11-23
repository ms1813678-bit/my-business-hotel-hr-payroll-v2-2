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
    # no system settings, no AI (leave viewing is allowed, read-only)
    for url in ("/settings/backup", "/settings/audit", "/settings/accounts", "/ai"):
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
        data={"csrf_token": csrf(page), "reason": "انتهاء مهمة", "target_username": "finance-user"},
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
        data={"csrf_token": csrf(page), "reason": "اختبار", "target_username": "guard-admin"},
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


def test_finance_leave_readonly_and_comprehensive_still_excluded(three_clients):
    _, finance, _ = three_clients
    # payroll, attendance and (new) leave reports are in the finance scope
    assert finance.get("/reports?report_type=payroll&period=this_month").status_code == 200
    assert finance.get("/reports?report_type=attendance&period=this_month").status_code == 200
    assert finance.get("/reports?report_type=leave&period=this_month").status_code == 200
    assert finance.get("/reports/view?report_type=leave&period=this_month").status_code == 200
    # the comprehensive export is still outside the finance scope
    assert finance.get("/reports?report_type=comprehensive&period=this_month", follow_redirects=False).status_code == 403
    assert finance.get("/reports/export.xlsx?start_date=2026-10-01&end_date=2026-10-31", follow_redirects=False).status_code == 403
    assert finance.get("/reports/export.pdf?start_date=2026-10-01&end_date=2026-10-31", follow_redirects=False).status_code == 403
    # the request form offers leave but still not comprehensive
    page = finance.get("/reports")
    assert 'value="leave"' in page.text
    assert 'value="comprehensive"' not in page.text
    assert 'value="payroll"' in page.text


def test_finance_leave_pages_readonly_writes_refused(three_clients):
    _, finance, _ = three_clients
    page = finance.get("/leave")
    assert page.status_code == 200, "finance may view leave and balances"
    assert 'action="/leave/records"' not in page.text, "no record form in the finance view"
    assert 'action="/leave/balances"' not in page.text, "no balance form in the finance view"
    assert 'action="/leave/types"' not in page.text, "no type form in the finance view"
    assert "/confirm" not in page.text and "اعتماد وخصم الرصيد" not in page.text
    # server-side: every leave write stays refused for finance
    assert finance.post("/leave/types", data={"code": "x", "name_ar": "x", "name_en": "x"}).status_code == 403
    assert finance.post("/leave/balances", data={}).status_code == 403
    assert finance.post("/leave/records", data={}).status_code == 403
    assert finance.post("/leave/records/1/confirm").status_code == 403
    assert finance.post("/leave/records/1/cancel", data={"reason": "x"}).status_code == 403


def test_assistant_reports_exclude_national_id(three_clients):
    _, _, assistant = three_clients
    # explicitly selecting the national-ID field is refused
    response = assistant.get(
        "/reports?report_type=attendance&period=this_month&fields=emp_id_proof",
        follow_redirects=False,
    )
    assert response.status_code == 403
    # the assistant's default attendance selection carries no identifier field
    response = assistant.get("/reports/view?report_type=attendance&period=this_month")
    assert response.status_code == 200
    assert "إثبات الهوية" not in response.text


def test_employee_photo_requires_employee_directory_access(three_clients):
    admin, finance, assistant = three_clients
    assert assistant.get("/employees/1/photo", follow_redirects=False).status_code == 403
    # finance and the sysadmin reach the endpoint; 404 only because no photo exists
    assert finance.get("/employees/1/photo").status_code == 404
    assert admin.get("/employees/1/photo").status_code == 404


def test_account_audit_actions_have_display_labels():
    from app.audit_display import ACTION_LABELS
    for action in ("account_created", "account_deactivated", "account_activated"):
        assert ACTION_LABELS.get(action, action) != action


def reset_account(client, account_id, reason="إعادة تعيين تجريبية", current_password=ADMIN_PW):
    page = client.get("/settings/accounts")
    return client.post(
        f"/settings/accounts/{account_id}/reset-password",
        data={"csrf_token": csrf(page), "reason": reason,
              "current_password": current_password, "target_username": "finance-user"},
    )


def test_admin_reset_issues_one_time_password_and_closes_old_sessions(three_clients):
    admin, finance, _ = three_clients
    assert finance.get("/payroll").status_code == 200, "finance session works before reset"

    with admin.test_session_factory() as db:
        target = db.scalar(select(AdminAccount).where(AdminAccount.username == "finance-user"))
        target_id = target.id
    response = reset_account(admin, target_id)
    assert response.status_code == 200, response.text
    match = re.search(r"<code>([^<]+)</code>", response.text)
    assert match, "the one-time password is revealed once in the response"
    new_password = match.group(1)
    assert len(new_password.replace("-", "")) >= 12

    with admin.test_session_factory() as db:
        account = db.scalar(select(AdminAccount).where(AdminAccount.id == target_id))
        assert account.password_hash != new_password, "stored only as a hash"
        audit = db.scalar(select(AuditLog).where(
            AuditLog.action == "account_password_reset").order_by(AuditLog.id.desc()))
        assert audit is not None and audit.actor == "guard-admin"
        assert audit.reason == "إعادة تعيين تجريبية"
        assert new_password not in (audit.new_value or "")

    assert finance.get("/payroll", follow_redirects=False).status_code in (303, 401), \
        "the old session is closed immediately"
    login(finance, "finance-user", new_password)
    page = finance.get("/settings/password")
    changed = finance.post(
        "/settings/password",
        data={
            "csrf_token": csrf(page),
            "current_password": new_password,
            "new_password": "Finance-Own-Password-456!",
            "confirm_password": "Finance-Own-Password-456!",
        },
    )
    assert changed.status_code == 200, changed.text
    assert finance.get("/payroll").status_code == 200, \
        "the one-time password works, and work resumes after the forced change"


def test_account_password_reset_needs_reason_and_admin(three_clients):
    admin, finance, assistant = three_clients
    with admin.test_session_factory() as db:
        target_id = db.scalar(
            select(AdminAccount.id).where(AdminAccount.username == "finance-user"))

    page = admin.get("/settings/accounts")
    token = csrf(page)
    missing = admin.post(
        f"/settings/accounts/{target_id}/reset-password",
        data={"csrf_token": token, "reason": "   ", "current_password": ADMIN_PW,
              "target_username": "finance-user"},
        follow_redirects=False,
    )
    assert missing.status_code == 303
    assert missing.headers["location"] == "/settings/accounts?error=missing"

    # without the administrator's own correct password nothing is reset
    wrong_auth = admin.post(
        f"/settings/accounts/{target_id}/reset-password",
        data={"csrf_token": token, "reason": "محاولة بكلمة غلط",
              "current_password": "not-the-admin-password", "target_username": "finance-user"},
        follow_redirects=False,
    )
    assert wrong_auth.status_code == 303
    assert wrong_auth.headers["location"] == "/settings/accounts?error=auth"
    with admin.test_session_factory() as db:
        assert db.scalar(select(AuditLog).where(
            AuditLog.action == "account_password_reset",
            AuditLog.reason == "محاولة بكلمة غلط")) is None

    forbidden = assistant.post(
        f"/settings/accounts/{target_id}/reset-password",
        data={"csrf_token": csrf(assistant.get("/settings/password")), "reason": "محاولة",
              "target_username": "finance-user"},
        follow_redirects=False,
    )
    assert forbidden.status_code == 403, "non-admin cannot reset passwords even with valid CSRF"


def test_reset_password_must_be_changed_before_anything_else(three_clients):
    admin, finance, _ = three_clients
    with admin.test_session_factory() as db:
        target_id = db.scalar(
            select(AdminAccount.id).where(AdminAccount.username == "finance-user"))
    response = reset_account(admin, target_id)
    new_password = re.search(r"<code>([^<]+)</code>", response.text).group(1)

    with admin.test_session_factory() as db:
        assert db.get(AdminAccount, target_id).must_change_password is True

    login_page = finance.get("/login")
    landing = finance.post(
        "/login",
        data={"username": "finance-user", "password": new_password,
              "csrf_token": csrf(login_page)},
        follow_redirects=False,
    )
    assert landing.status_code == 303
    assert landing.headers["location"] == "/settings/password?forced=1"

    blocked = finance.get("/payroll", headers={"accept": "text/html"}, follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/settings/password?forced=1"

    page = finance.get("/settings/password")
    assert page.status_code == 200, "the password page itself stays reachable"
    changed = finance.post(
        "/settings/password",
        data={
            "csrf_token": csrf(page),
            "current_password": new_password,
            "new_password": "Finance-Own-New-Password-99!",
            "confirm_password": "Finance-Own-New-Password-99!",
        },
    )
    assert changed.status_code == 200, changed.text
    with admin.test_session_factory() as db:
        assert db.get(AdminAccount, target_id).must_change_password is False
    assert finance.get("/payroll").status_code == 200, "normal work resumes after the change"


def test_reset_reveal_response_is_never_cached(three_clients):
    admin, _, _ = three_clients
    with admin.test_session_factory() as db:
        target_id = db.scalar(
            select(AdminAccount.id).where(AdminAccount.username == "finance-user"))
    response = reset_account(admin, target_id)
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"], \
        "the one-time password must not survive in any browser cache"
    assert response.headers.get("pragma") == "no-cache"
    # a fresh visit to the accounts page shows no password again
    again = admin.get("/settings/accounts")
    assert again.status_code == 200
    assert "<code>" not in again.text, "the reveal exists only in the original response"


def test_only_system_admin_reaches_any_backup_entrypoint(three_clients):
    """Owner decision (2026-11-23, item 85): ONLY the system administrator may
    extract or manage full-system backups — every entry point, server-side,
    not just hidden buttons. Finance and assistant manager are refused on the
    page and on every mutation, with valid CSRF tokens (so the refusal is the
    role check, not the CSRF check)."""
    _, finance, assistant = three_clients
    for client in (finance, assistant):
        token = csrf(client.get("/settings/password"))
        assert client.get("/settings/backup", follow_redirects=False).status_code == 403
        assert client.post("/settings/backup/create", data={
            "csrf_token": token, "backup_type": "manual"}, follow_redirects=False).status_code == 403
        assert client.post("/settings/backup/restore", data={
            "csrf_token": token, "backup_id": "1", "confirm_text": "RESTORE",
            "reason": "x", "passphrase": "", "current_password": "x"},
            follow_redirects=False).status_code == 403
        assert client.post("/settings/backup/root", data={
            "csrf_token": token, "backup_root": "/tmp/x"}, follow_redirects=False).status_code == 403
        assert client.post("/settings/backup/passphrase", data={
            "csrf_token": token, "new_passphrase": "x" * 12,
            "confirm_passphrase": "x" * 12, "current_passphrase": ""},
            follow_redirects=False).status_code == 403


def test_finance_cannot_bulk_approve_attendance(three_clients):
    _, finance, _ = three_clients
    page = finance.get("/attendance/review")
    assert page.status_code == 200
    # read-only: no bulk form rendered for finance
    assert "approve-all-matching" not in page.text
    # valid CSRF token from a page finance can write on: refusal must come from the role guard
    token = csrf(finance.get("/advances"))
    response = finance.post(
        "/attendance/approve-all-matching",
        data={"csrf_token": token, "reason": "مصادر متطابقة"},
        follow_redirects=False,
    )
    assert response.status_code == 403
