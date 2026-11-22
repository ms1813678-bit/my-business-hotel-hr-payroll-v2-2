import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from decimal import Decimal

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, Department, Employee, EmployeeCodeCounter
from app.security import hash_password


class _ArabicUiTextProbe(HTMLParser):
    _SKIP_TAGS = {"script", "style", "textarea", "pre", "code"}
    _ARABIC = re.compile(r"[\u0600-\u06FF]")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_stack = []
        self.arabic_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self.skip_stack.append(tag)
        for name, value in attrs:
            if name.lower() in {"placeholder", "title", "aria-label", "alt"} and value:
                if self._ARABIC.search(value):
                    self.arabic_parts.append(f"{name}={value}")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self.skip_stack and self.skip_stack[-1] == tag:
            self.skip_stack.pop()

    def handle_data(self, data):
        if not self.skip_stack and self._ARABIC.search(data):
            value = data.strip()
            if value:
                self.arabic_parts.append(value)


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    with TestingSessionLocal() as db:
        admin = AdminAccount(
            username="integration-admin",
            password_hash=hash_password("Integration-Test-Password-123!"),
            active=True,
        )
        department = Department(name="Test Department", active=True)
        db.add_all([admin, department, EmployeeCodeCounter(id=1, last_number=0)])
        db.commit()
        db.refresh(admin)
        db.refresh(department)
        department_id = department.id
        admin_id = admin.id

    def override_get_db():
        with TestingSessionLocal() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", TestingSessionLocal)
    # The route tests below verify HTML/CSRF/routing behavior, not the
    # persistent conversation repository. Stub the imported route helper so
    # these tests never fall through to the CI PostgreSQL database. The
    # gateway's persistence is covered by its own database-level tests.
    monkeypatch.setattr("app.main.recent_memory", lambda actor, limit=12: [])

    with TestClient(app) as test_client:
        test_client.test_department_id = department_id
        test_client.test_admin_id = admin_id
        yield test_client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def csrf_from(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, "CSRF hidden field was not rendered"
    return match.group(1)


def login(client):
    page = client.get("/login")
    token = csrf_from(page)
    response = client.post(
        "/login",
        data={
            "username": "integration-admin",
            "password": "Integration-Test-Password-123!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    return response


def test_anonymous_browser_is_redirected_to_login_for_protected_pages(client):
    for path in ("/", "/employees"):
        response = client.get(path, headers={"accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_api_request_keeps_401_for_unauthenticated_user(client):
    response = client.get("/", headers={"accept": "application/json"})
    assert response.status_code == 401
    assert response.json()["detail"]


def test_missing_session_cookie_redirects_browser_to_login(client):
    """A removed cookie tests an absent session, not natural cookie expiration."""
    assert login(client).status_code == 303
    # Removing the cookie tests an absent session, not expiration of a still-present cookie.
    client.cookies.delete("session")
    response = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_anonymous_user_cannot_open_protected_pages_or_post(client):
    assert client.get("/").status_code == 401
    assert client.get("/employees").status_code == 401
    assert client.post("/employees", data={}).status_code == 403
    anonymous_page = client.get("/login")
    anonymous_token = csrf_from(anonymous_page)
    assert client.post(
        "/employees",
        data={"csrf_token": anonymous_token},
    ).status_code == 401
    assert client.post("/logout", data={}).status_code == 403


@pytest.mark.parametrize("path", ["/login", "/logout", "/employees", "/attendance/months/1/approve"])
def test_post_routes_reject_missing_or_invalid_csrf(client, path):
    if path == "/logout":
        assert login(client).status_code == 303
    for token in ("", "invalid-csrf-token"):
        response = client.post(path, data={"csrf_token": token})
        assert response.status_code == 403


def test_login_and_dashboard_require_valid_credentials_and_csrf(client):
    page = client.get("/login")
    token = csrf_from(page)

    missing_token = client.post(
        "/login",
        data={"username": "integration-admin", "password": "Integration-Test-Password-123!"},
    )
    assert missing_token.status_code == 403

    bad_password = client.post(
        "/login",
        data={
            "username": "integration-admin",
            "password": "Wrong-Integration-Password-456!",
            "csrf_token": token,
        },
    )
    assert bad_password.status_code == 401
    assert "csrf_token" in bad_password.text

    success = login(client)
    assert success.status_code == 303
    assert success.headers["location"] == "/"
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "integration-admin" in dashboard.text


@pytest.mark.parametrize("change", ["deactivate", "delete"])
def test_sole_admin_cannot_be_deactivated_or_deleted(client, change):
    from sqlalchemy.exc import IntegrityError

    assert login(client).status_code == 303
    from app.main import SessionLocal

    with SessionLocal() as db:
        admin = db.get(AdminAccount, client.test_admin_id)
        if change == "deactivate":
            admin.active = False
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
        else:
            db.delete(admin)
            with pytest.raises(ValueError, match="cannot be deleted"):
                db.flush()
            db.rollback()

    # A failed destructive operation must not lock the only administrator out.
    response = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 200
    assert "integration-admin" in response.text


def test_employee_creation_requires_auth_and_valid_csrf(client):
    assert login(client).status_code == 303
    page = client.get("/employees")
    assert page.status_code == 200
    token = csrf_from(page)

    data = {
        "csrf_token": token,
        "full_name": "Synthetic Test Employee",
        "national_id": "",
        "phone": "",
        "department_id": str(client.test_department_id),
        "position": "Test Position",
        "hire_date": date.today().isoformat(),
        "basic_salary": str(Decimal("1000.00")),
        "daily_hours": "8",
        "fingerprint_id": "",
    }
    rejected = client.post(
        "/employees",
        data={**data, "csrf_token": "wrong-integration-csrf-token"},
    )
    assert rejected.status_code == 403

    response = client.post("/employees", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/employees"

    from app.main import SessionLocal

    with SessionLocal() as db:
        employee = db.query(Employee).filter_by(full_name="Synthetic Test Employee").one()
        assert employee.code == "MY-0001"


def test_employee_nationality_and_passport_rules(client):
    assert login(client).status_code == 303
    page = client.get("/employees")
    token = csrf_from(page)

    base = {
        "csrf_token": token,
        "full_name": "Synthetic Foreign Employee",
        "national_id": "",
        "phone": "",
        "department_id": str(client.test_department_id),
        "position": "Test Position",
        "hire_date": date.today().isoformat(),
        "basic_salary": str(Decimal("1000.00")),
        "daily_hours": "8",
        "fingerprint_id": "",
    }
    missing_passport = client.post(
        "/employees",
        data={**base, "nationality": "foreign", "passport_number": ""},
        follow_redirects=False,
    )
    assert missing_passport.status_code == 400

    created = client.post(
        "/employees",
        data={**base, "nationality": "foreign", "passport_number": "A12345678"},
        follow_redirects=False,
    )
    assert created.status_code == 303

    duplicate_passport = client.post(
        "/employees",
        data={**base, "full_name": "Synthetic Foreign Employee 2", "nationality": "foreign", "passport_number": "A12345678"},
        follow_redirects=False,
    )
    assert duplicate_passport.status_code == 409

    egyptian_with_passport = client.post(
        "/employees",
        data={**base, "full_name": "Synthetic Egyptian Employee", "nationality": "egyptian", "national_id": "29803100201134", "passport_number": "B999"},
        follow_redirects=False,
    )
    assert egyptian_with_passport.status_code == 303

    from app.main import SessionLocal

    with SessionLocal() as db:
        foreign = db.query(Employee).filter_by(full_name="Synthetic Foreign Employee").one()
        assert foreign.nationality == "foreign"
        assert foreign.passport_number == "A12345678"
        egyptian = db.query(Employee).filter_by(full_name="Synthetic Egyptian Employee").one()
        assert egyptian.nationality == "egyptian"
        assert egyptian.national_id == "29803100201134"
        assert egyptian.passport_number is None


def test_attendance_review_page_is_paginated(client):
    assert login(client).status_code == 303
    from app.main import SessionLocal
    from app.models import AttendanceDailyRecord

    token = csrf_from(client.get("/employees"))
    created = client.post(
        "/employees",
        data={
            "csrf_token": token,
            "full_name": "Attendance Pagination Employee",
            "department_id": str(client.test_department_id),
            "position": "Test Position",
            "hire_date": "2025-01-01",
            "basic_salary": "1000.00",
            "daily_hours": "8",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    with SessionLocal() as db:
        employee = db.query(Employee).filter_by(
            full_name="Attendance Pagination Employee"
        ).one()
        employee_id = employee.id
        for index in range(105):
            db.add(AttendanceDailyRecord(
                employee_id=employee_id,
                work_date=date(2026, 11, 1) + timedelta(days=index),
                status="needs_review",
            ))
        db.commit()

    first = client.get("/attendance/review")
    assert first.status_code == 200
    assert "<span>عرض</span> 100 / 105 <span>سجلًا معلقًا</span>" in first.text
    assert "<span>صفحة</span> 1 / 2" in first.text
    assert 'href="/attendance/review?page=2"' in first.text
    second = client.get("/attendance/review?page=2")
    assert second.status_code == 200
    assert "<span>عرض</span> 5 / 105 <span>سجلًا معلقًا</span>" in second.text
    assert "<span>صفحة</span> 2 / 2" in second.text



def test_manual_attendance_is_prefilled_and_visible_in_review(client):
    assert login(client).status_code == 303
    from app.main import SessionLocal
    from app.models import AttendanceMonth

    token = csrf_from(client.get("/employees"))
    created = client.post(
        "/employees",
        data={
            "csrf_token": token,
            "full_name": "Manual Attendance Review Employee",
            "department_id": str(client.test_department_id),
            "position": "Test Position",
            "hire_date": "2025-01-01",
            "basic_salary": "1000.00",
            "daily_hours": "8",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    with SessionLocal.begin() as db:
        db.add(AttendanceMonth(
            period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 30),
            status="under_review",
            created_by="integration-admin",
        ))

    token = csrf_from(client.get("/attendance/review"))
    response = client.post(
        "/attendance/manual",
        data={
            "csrf_token": token,
            "employee_id": "1",
            "work_date": "2026-11-19",
            "clock_in": "2026-11-19T08:05",
            "clock_out": "2026-11-19T16:10",
            "reason": "Manual correction approved for review test",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    review = client.get("/attendance/review")
    assert review.status_code == 200
    assert 'value="2026-11-19T08:05"' in review.text
    assert 'value="2026-11-19T16:10"' in review.text
    assert "Manual correction approved for review test" in review.text
    assert "integration-admin" in review.text

def test_logout_requires_csrf_and_clears_session(client):
    assert login(client).status_code == 303
    dashboard = client.get("/")
    token = csrf_from(dashboard)

    rejected = client.post("/logout", data={"csrf_token": "wrong-token"})
    assert rejected.status_code == 403

    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/").status_code == 401


def _session_middleware_options():
    middleware = next(
        item for item in app.user_middleware if item.cls.__name__ == "SessionMiddleware"
    )
    return middleware.kwargs


def test_session_cookie_has_expected_lifetime_and_security_attributes(client):
    response = client.get("/login")
    cookie = response.headers["set-cookie"]
    options = _session_middleware_options()

    assert options["max_age"] == 8 * 60 * 60 == 28800
    assert "session=" in cookie
    assert "Max-Age=28800" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert options["same_site"] == "lax"


def test_session_cookie_secure_flag_follows_production_environment(monkeypatch):
    import importlib
    import os

    from app import config as config_module
    from app import main as main_module

    original_app_env = os.environ.get("APP_ENV")
    monkeypatch.setenv("APP_ENV", "production")

    try:
        # Reload settings so Pydantic reads APP_ENV from the real process environment.
        production_config = importlib.reload(config_module)
        assert production_config.settings.app_env.lower() == "production"

        # Rebuild the actual application using the freshly loaded production settings.
        production_main = importlib.reload(main_module)
        assert production_main.settings is production_config.settings

        middleware = next(
            item
            for item in production_main.app.user_middleware
            if item.cls.__name__ == "SessionMiddleware"
        )
        assert middleware.kwargs["https_only"] is True

        with TestClient(production_main.app) as production_client:
            response = production_client.get("/login")

        production_cookie = response.headers["set-cookie"].lower()
        assert "secure" in production_cookie
        assert "httponly" in production_cookie
        assert "samesite=lax" in production_cookie
        assert "max-age=28800" in production_cookie
    finally:
        if original_app_env is None:
            monkeypatch.delenv("APP_ENV", raising=False)
        else:
            monkeypatch.setenv("APP_ENV", original_app_env)

        # Restore module-level settings and app construction for other tests.
        importlib.reload(config_module)
        importlib.reload(main_module)


def test_login_rotates_csrf_token_and_rejects_pre_login_token(client):
    anonymous_page = client.get("/login")
    old_token = csrf_from(anonymous_page)

    successful_login = client.post(
        "/login",
        data={
            "username": "integration-admin",
            "password": "Integration-Test-Password-123!",
            "csrf_token": old_token,
        },
        follow_redirects=False,
    )
    assert successful_login.status_code == 303

    dashboard = client.get("/")
    new_token = csrf_from(dashboard)
    assert new_token != old_token

    old_token_response = client.post("/logout", data={"csrf_token": old_token})
    assert old_token_response.status_code == 403

    new_token_response = client.post(
        "/logout",
        data={"csrf_token": new_token},
        follow_redirects=False,
    )
    assert new_token_response.status_code == 303
    assert new_token_response.headers["location"] == "/login"



def test_admin_session_is_invalidated_when_session_version_changes(client):
    assert login(client).status_code == 303
    from app.main import SessionLocal

    from app.auth import change_admin_password

    with SessionLocal.begin() as db:
        admin = db.get(AdminAccount, client.test_admin_id)
        change_admin_password(db, admin, "Changed-Integration-Password-789!")

    response = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_employee_code_counter_is_persistent_and_never_reuses_reserved_number(client):
    from app.employee_codes import allocate_employee_code
    from app.main import SessionLocal

    with SessionLocal.begin() as db:
        assert allocate_employee_code(db) == "MY-0001"
    with SessionLocal.begin() as db:
        assert allocate_employee_code(db) == "MY-0002"
    # A fresh database session observes the committed counter value.
    with SessionLocal.begin() as db:
        assert allocate_employee_code(db) == "MY-0003"



def test_employee_archive_and_rehire_preserve_service_history(client):
    assert login(client).status_code == 303
    page = client.get("/employees")
    token = csrf_from(page)
    employee_data = {
        "csrf_token": token,
        "full_name": "Service History Test",
        "national_id": "",
        "phone": "",
        "department_id": str(client.test_department_id),
        "position": "Test Position",
        "hire_date": "2025-01-10",
        "basic_salary": "1000.00",
        "daily_hours": "8",
        "fingerprint_id": "",
    }
    assert client.post("/employees", data=employee_data, follow_redirects=False).status_code == 303

    from app.main import SessionLocal
    from app.models import EmployeeServicePeriod

    with SessionLocal() as db:
        employee = db.query(Employee).filter_by(full_name="Service History Test").one()
        employee_id = employee.id
        employee_code = employee.code

    archive = client.post(
        f"/employees/{employee_id}/archive",
        data={"csrf_token": token, "reason": "End of first service"},
        follow_redirects=False,
    )
    assert archive.status_code == 303

    rehire = client.post(
        f"/employees/{employee_id}/rehire",
        data={
            "csrf_token": token,
            "new_hire_date": (date.today() + timedelta(days=1)).isoformat(),
            "reason": "Rehired after separation",
        },
        follow_redirects=False,
    )
    assert rehire.status_code == 303

    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        periods = (
            db.query(EmployeeServicePeriod)
            .filter_by(employee_id=employee_id)
            .order_by(EmployeeServicePeriod.id)
            .all()
        )
        assert employee.code == employee_code
        assert employee.status == "active"
        assert employee.hire_date == date.today() + timedelta(days=1)
        assert len(periods) == 2
        assert periods[0].end_date == date.today()
        assert periods[0].end_reason == "End of first service"
        assert periods[1].start_date == date.today() + timedelta(days=1)


def test_employee_profile_edit_records_effective_dated_history_and_audit(client):
    from app.main import SessionLocal
    from app.models import AuditLog, Branch, EmployeeChange

    assert login(client).status_code == 303
    with SessionLocal.begin() as db:
        branch = Branch(
            department_id=client.test_department_id,
            name="Test Branch",
            active=True,
        )
        db.add(branch)
        db.flush()
        branch_id = branch.id

    token = csrf_from(client.get("/employees"))
    create_data = {
        "csrf_token": token,
        "full_name": "Profile Edit Test",
        "national_id": "29001011501101",
        "phone": "01000000000",
        "address": "Old address",
        "department_id": str(client.test_department_id),
        "branch_id": str(branch_id),
        "position": "Old Position",
        "hire_date": "2025-01-01",
        "basic_salary": "1000.00",
        "daily_hours": "8",
        "fingerprint_id": "TEST-FP-1001",
    }
    created = client.post("/employees", data=create_data, follow_redirects=False)
    assert created.status_code == 303

    with SessionLocal() as db:
        employee = db.query(Employee).filter_by(full_name="Profile Edit Test").one()
        employee_id = employee.id

    edit_data = {
        "csrf_token": token,
        "full_name": "Profile Edit Test Updated",
        "national_id": "29001011501101",
        "phone": "01111111111",
        "address": "New address",
        "department_id": str(client.test_department_id),
        "branch_id": str(branch_id),
        "position": "New Position",
        "basic_salary": "1250.50",
        "daily_hours": "7.5",
        "fingerprint_id": "TEST-FP-1001",
        "effective_date": "2025-02-01",
        "reason": "Approved employee data correction",
    }
    edited = client.post(
        f"/employees/{employee_id}/edit",
        data=edit_data,
        follow_redirects=False,
    )
    assert edited.status_code == 303

    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        changes = (
            db.query(EmployeeChange)
            .filter_by(employee_id=employee_id)
            .all()
        )
        audit = (
            db.query(AuditLog)
            .filter_by(entity_type="employee", entity_id=str(employee_id), action="employee_updated")
            .one()
        )
        assert employee.full_name == "Profile Edit Test Updated"
        assert employee.address == "New address"
        assert employee.phone == "01111111111"
        assert employee.basic_salary == Decimal("1250.50")
        assert employee.daily_hours == Decimal("7.50")
        assert {change.field_name for change in changes} == {
            "full_name", "phone", "address", "position", "basic_salary", "daily_hours"
        }
        assert all(change.effective_date == date(2025, 2, 1) for change in changes)
        assert all(change.reason == "Approved employee data correction" for change in changes)
        assert all(change.old_value is not None and change.new_value is not None for change in changes)
        assert audit.actor == "integration-admin"
        assert "Approved employee data correction" == audit.reason


def test_employee_profile_edit_rejects_future_effective_date(client):
    from app.main import SessionLocal
    from app.models import EmployeeChange

    assert login(client).status_code == 303
    with SessionLocal.begin() as db:
        employee = Employee(
            code="MY-FUTURE-EFFECTIVE-01",
            full_name="Future Effective Test",
            department_id=client.test_department_id,
            position="Test Position",
            hire_date=date(2024, 1, 1),
            status="active",
            basic_salary=Decimal("5000.00"),
            daily_hours=Decimal("8.00"),
        )
        db.add(employee)
        db.flush()
        employee_id = employee.id

    token = csrf_from(client.get("/employees"))
    response = client.post(
        f"/employees/{employee_id}/edit",
        data={
            "csrf_token": token,
            "full_name": "Future Effective Test Updated",
            "national_id": "",
            "phone": "",
            "address": "",
            "department_id": str(client.test_department_id),
            "branch_id": "",
            "position": "Test Position",
            "basic_salary": "7000.00",
            "daily_hours": "8.00",
            "fingerprint_id": "",
            "effective_date": "2099-01-01",
            "reason": "Future date must not change current employee values early",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        assert employee.basic_salary == Decimal("5000.00")
        assert employee.full_name == "Future Effective Test"
        assert db.query(EmployeeChange).filter_by(employee_id=employee_id).count() == 0


def test_employee_edit_rejects_branch_from_another_department(client):
    from app.main import SessionLocal
    from app.models import Branch

    assert login(client).status_code == 303
    with SessionLocal.begin() as db:
        other_department = Department(name="Other Test Department", active=True)
        db.add(other_department)
        db.flush()
        wrong_branch = Branch(
            department_id=other_department.id,
            name="Wrong Department Branch",
            active=True,
        )
        db.add(wrong_branch)
        db.flush()
        wrong_branch_id = wrong_branch.id

    token = csrf_from(client.get("/employees"))
    create_data = {
        "csrf_token": token,
        "full_name": "Branch Validation Test",
        "department_id": str(client.test_department_id),
        "position": "Test Position",
        "hire_date": date.today().isoformat(),
        "basic_salary": "1000.00",
        "daily_hours": "8",
    }
    assert client.post("/employees", data=create_data, follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        employee = db.query(Employee).filter_by(full_name="Branch Validation Test").one()
        employee_id = employee.id

    response = client.post(
        f"/employees/{employee_id}/edit",
        data={
            "csrf_token": token,
            "full_name": "Branch Validation Test",
            "department_id": str(client.test_department_id),
            "branch_id": str(wrong_branch_id),
            "position": "Test Position",
            "basic_salary": "1000.00",
            "daily_hours": "8",
            "effective_date": date.today().isoformat(),
            "reason": "Testing branch ownership validation",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_department_branch_management_and_employee_transfer_history(client):
    from app.main import SessionLocal
    from app.models import AuditLog, Branch, EmployeeChange

    assert login(client).status_code == 303
    token = csrf_from(client.get("/employees"))

    created_department = client.post(
        "/departments",
        data={"csrf_token": token, "name": "Transfer Destination"},
        follow_redirects=False,
    )
    assert created_department.status_code == 303

    with SessionLocal() as db:
        destination = db.query(Department).filter_by(name="Transfer Destination").one()
        destination_id = destination.id

    created_branch = client.post(
        "/branches",
        data={"csrf_token": token, "department_id": destination_id, "name": "Destination Branch"},
        follow_redirects=False,
    )
    assert created_branch.status_code == 303

    with SessionLocal() as db:
        branch = db.query(Branch).filter_by(name="Destination Branch").one()
        branch_id = branch.id

    created_employee = client.post(
        "/employees",
        data={
            "csrf_token": token,
            "full_name": "Transfer History Test",
            "department_id": client.test_department_id,
            "position": "Test Position",
            "hire_date": "2025-01-01",
            "basic_salary": "1000.00",
            "daily_hours": "8",
        },
        follow_redirects=False,
    )
    assert created_employee.status_code == 303
    with SessionLocal() as db:
        employee = db.query(Employee).filter_by(full_name="Transfer History Test").one()
        employee_id = employee.id

    moved = client.post(
        f"/employees/{employee_id}/transfer",
        data={
            "csrf_token": token,
            "department_id": destination_id,
            "branch_id": branch_id,
            "effective_date": "2025-03-01",
            "reason": "Approved department transfer",
        },
        follow_redirects=False,
    )
    assert moved.status_code == 303

    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        changes = db.query(EmployeeChange).filter_by(employee_id=employee_id).all()
        audit = db.query(AuditLog).filter_by(
            entity_type="employee",
            entity_id=str(employee_id),
            action="employee_transferred",
        ).one()
        assert employee.department_id == destination_id
        assert employee.branch_id == branch_id
        assert {item.field_name for item in changes} == {"department_id", "branch_id"}
        assert all(item.effective_date == date(2025, 3, 1) for item in changes)
        assert all(item.reason == "Approved department transfer" for item in changes)
        assert audit.reason == "Approved department transfer"


def test_branch_cannot_be_deactivated_while_active_employee_is_assigned(client):
    from app.main import SessionLocal
    from app.models import Branch

    assert login(client).status_code == 303
    with SessionLocal.begin() as db:
        branch = Branch(
            department_id=client.test_department_id,
            name="Occupied Branch",
            active=True,
        )
        db.add(branch)
        db.flush()
        branch_id = branch.id

    token = csrf_from(client.get("/employees"))
    created = client.post(
        "/employees",
        data={
            "csrf_token": token,
            "full_name": "Occupied Branch Employee",
            "department_id": client.test_department_id,
            "branch_id": branch_id,
            "position": "Test Position",
            "hire_date": date.today().isoformat(),
            "basic_salary": "1000.00",
            "daily_hours": "8",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    response = client.post(
        f"/branches/{branch_id}/edit",
        data={
            "csrf_token": token,
            "department_id": client.test_department_id,
            "name": "Occupied Branch",
        },
        follow_redirects=False,
    )
    assert response.status_code == 409


def test_employee_creation_audits_record_and_rejects_duplicate_identifiers(client):
    from app.main import SessionLocal
    from app.models import AuditLog

    assert login(client).status_code == 303
    token = csrf_from(client.get("/employees"))
    data = {
        "csrf_token": token,
        "full_name": "Unique Employee",
        "national_id": "29001011502001",
        "phone": "01000000001",
        "address": "Hotel address",
        "department_id": str(client.test_department_id),
        "position": "Receptionist",
        "hire_date": "2025-01-01",
        "basic_salary": "5000.00",
        "daily_hours": "8",
        "fingerprint_id": "UNIQUE-FP-2001",
    }
    created = client.post("/employees", data=data, follow_redirects=False)
    assert created.status_code == 303
    with SessionLocal() as db:
        employee = db.query(Employee).filter_by(national_id="29001011502001").one()
        employee_id = employee.id
        code = employee.code
        audit = db.query(AuditLog).filter_by(
            entity_type="employee", entity_id=str(employee_id), action="employee_created"
        ).one()
        assert audit.actor == "integration-admin"
        assert code == "MY-0001"

    duplicate = dict(data, full_name="Duplicate National ID", fingerprint_id="UNIQUE-FP-2002")
    response = client.post("/employees", data=duplicate, follow_redirects=False)
    assert response.status_code == 409

    duplicate_fp = dict(data, full_name="Duplicate Fingerprint", national_id="29001011502002")
    response = client.post("/employees", data=duplicate_fp, follow_redirects=False)
    assert response.status_code == 409

    with SessionLocal() as db:
        assert db.query(Employee).count() == 1


def test_employee_probation_dates_are_optional_validated_and_audited(client):
    from app.main import SessionLocal
    from app.models import EmployeeChange

    assert login(client).status_code == 303
    token = csrf_from(client.get("/employees"))
    created = client.post(
        "/employees",
        data={
            "csrf_token": token,
            "full_name": "Probation Dates Test",
            "department_id": client.test_department_id,
            "position": "Test Position",
            "hire_date": "2025-01-01",
            "probation_start_date": "2025-01-01",
            "probation_end_date": "2025-03-31",
            "basic_salary": "1000.00",
            "daily_hours": "8",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    with SessionLocal() as db:
        employee = db.query(Employee).filter_by(full_name="Probation Dates Test").one()
        employee_id = employee.id
        assert employee.probation_start_date == date(2025, 1, 1)
        assert employee.probation_end_date == date(2025, 3, 31)

    invalid = client.post(
        f"/employees/{employee_id}/edit",
        data={
            "csrf_token": token,
            "full_name": "Probation Dates Test",
            "department_id": client.test_department_id,
            "position": "Test Position",
            "basic_salary": "1000.00",
            "daily_hours": "8",
            "probation_start_date": "2025-04-01",
            "probation_end_date": "2025-03-01",
            "effective_date": "2025-04-01",
            "reason": "Invalid probation range",
        },
        follow_redirects=False,
    )
    assert invalid.status_code == 400

    updated = client.post(
        f"/employees/{employee_id}/edit",
        data={
            "csrf_token": token,
            "full_name": "Probation Dates Test",
            "department_id": client.test_department_id,
            "position": "Test Position",
            "basic_salary": "1000.00",
            "daily_hours": "8",
            "probation_start_date": "2025-01-01",
            "probation_end_date": "2025-04-30",
            "effective_date": "2025-04-01",
            "reason": "Probation period extended",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        changes = db.query(EmployeeChange).filter_by(
            employee_id=employee_id,
            field_name="probation_end_date",
        ).all()
        assert employee.probation_end_date == date(2025, 4, 30)
        assert len(changes) == 1
        assert changes[0].effective_date == date(2025, 4, 1)
        assert changes[0].reason == "Probation period extended"


def test_employee_data_change_request_can_be_reviewed_and_applied(client):
    from app.main import SessionLocal
    from app.models import AuditLog, EmployeeChange, EmployeeDataChangeRequest

    assert login(client).status_code == 303
    token = csrf_from(client.get("/employees"))
    created = client.post(
        "/employees",
        data={
            "csrf_token": token,
            "full_name": "Change Request Employee",
            "department_id": client.test_department_id,
            "position": "Receptionist",
            "hire_date": "2025-01-01",
            "basic_salary": "1000.00",
            "daily_hours": "8",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    with SessionLocal() as db:
        employee = db.query(Employee).filter_by(full_name="Change Request Employee").one()
        employee_id = employee.id

    submitted = client.post(
        f"/employees/{employee_id}/data-change-requests",
        data={
            "csrf_token": token,
            "field_name": "phone",
            "requested_value": "01012345678",
            "request_reason": "Employee requested phone correction",
        },
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    with SessionLocal() as db:
        request_row = db.query(EmployeeDataChangeRequest).filter_by(employee_id=employee_id).one()
        request_id = request_row.id
        assert request_row.status == "pending"

    reviewed = client.post(
        f"/employee-data-change-requests/{request_id}/review",
        data={
            "csrf_token": token,
            "decision": "apply",
            "decision_reason": "Identity checked and approved",
            "effective_date": "2025-02-01",
        },
        follow_redirects=False,
    )
    assert reviewed.status_code == 303
    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        request_row = db.get(EmployeeDataChangeRequest, request_id)
        change = db.query(EmployeeChange).filter_by(
            employee_id=employee_id, field_name="phone"
        ).one()
        audit = db.query(AuditLog).filter_by(
            entity_type="employee_data_change_request",
            entity_id=str(request_id),
            action="employee_data_change_applied",
        ).one()
        assert employee.phone == "01012345678"
        assert request_row.status == "applied"
        assert request_row.reviewed_by == "integration-admin"
        assert change.old_value is None
        assert change.new_value == "01012345678"
        assert change.effective_date == date(2025, 2, 1)
        assert audit.reason == "Identity checked and approved"

    repeated = client.post(
        f"/employee-data-change-requests/{request_id}/review",
        data={
            "csrf_token": token,
            "decision": "reject",
            "decision_reason": "Duplicate review",
        },
        follow_redirects=False,
    )
    assert repeated.status_code == 409


def test_employee_photo_upload_is_validated_and_served_only_to_admin(client, monkeypatch, tmp_path):
    import io
    from PIL import Image
    import app.main as main_module

    monkeypatch.setattr(main_module, "EMPLOYEE_PHOTO_DIR", tmp_path)
    # Photo content must remain private even when the URL is known.
    anonymous = client.get("/employees/1/photo")
    assert anonymous.status_code == 401
    assert login(client).status_code == 303
    token = csrf_from(client.get("/employees"))

    image_bytes = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(image_bytes, format="PNG")
    created = client.post(
        "/employees",
        data={
            "csrf_token": token,
            "full_name": "Photo Employee",
            "department_id": client.test_department_id,
            "position": "Test Position",
            "hire_date": "2025-01-01",
            "basic_salary": "1000.00",
            "daily_hours": "8",
        },
        files={"photo": ("employee.png", image_bytes.getvalue(), "image/png")},
        follow_redirects=False,
    )
    assert created.status_code == 303
    with main_module.SessionLocal() as db:
        employee = db.query(Employee).filter_by(full_name="Photo Employee").one()
        employee_id = employee.id
        photo_path = employee.photo_path
        assert photo_path.endswith(".png")
        assert (tmp_path / photo_path).is_file()

    served = client.get(f"/employees/{employee_id}/photo")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"

    invalid = client.post(
        "/employees",
        data={
            "csrf_token": token,
            "full_name": "Invalid Photo Employee",
            "department_id": client.test_department_id,
            "position": "Test Position",
            "hire_date": "2025-01-01",
            "basic_salary": "1000.00",
            "daily_hours": "8",
        },
        files={"photo": ("not-image.png", b"not an image", "image/png")},
        follow_redirects=False,
    )
    assert invalid.status_code == 400


def test_employee_photo_exif_orientation_is_applied(client, monkeypatch, tmp_path):
    import io
    from PIL import Image
    import app.main as main_module

    monkeypatch.setattr(main_module, "EMPLOYEE_PHOTO_DIR", tmp_path)
    assert login(client).status_code == 303
    token = csrf_from(client.get("/employees"))

    # A 200x100 photo whose EXIF says "rotate 90° CW" (phone portrait shot).
    image_bytes = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (200, 100), "white").save(image_bytes, format="JPEG", exif=exif)
    created = client.post(
        "/employees",
        data={
            "csrf_token": token,
            "full_name": "EXIF Employee",
            "department_id": client.test_department_id,
            "position": "Test Position",
            "hire_date": "2025-01-01",
            "basic_salary": "1000.00",
            "daily_hours": "8",
        },
        files={"photo": ("phone-shot.jpg", image_bytes.getvalue(), "image/jpeg")},
        follow_redirects=False,
    )
    assert created.status_code == 303
    with main_module.SessionLocal() as db:
        employee = db.query(Employee).filter_by(full_name="EXIF Employee").one()
        stored = tmp_path / employee.photo_path
    with Image.open(stored) as saved:
        assert saved.size == (100, 200)  # pixels physically rotated
        assert not saved.getexif() or saved.getexif().get(0x0112) in (None, 1)


def _employee_form(token, **overrides):
    data = {
        "csrf_token": token,
        "full_name": "ID Test Employee",
        "national_id": "",
        "nationality": "egyptian",
        "passport_number": "",
        "phone": "",
        "address": "",
        "department_id": "1",
        "branch_id": "",
        "position": "Test Position",
        "hire_date": "2025-01-01",
        "probation_start_date": "",
        "probation_end_date": "",
        "basic_salary": "1000",
        "daily_hours": "8",
        "fingerprint_id": "",
    }
    data.update(overrides)
    return data


def test_national_id_structure_validation_rules():
    from app.national_id import is_valid_egyptian_national_id as valid

    assert valid("29001011501234")  # 1990-01-01, Kafr El Sheikh (15)
    assert valid("30101010123456")  # 2001-01-01, Cairo (01)
    assert valid("28802298801234")  # 1988-02-29 leap day, Luxor (29)
    assert valid("30005018801234")  # born-abroad code 88
    assert not valid("2900101150123")   # too short
    assert not valid("2900101150123a")  # non-digit
    assert not valid("49001011501234")  # century digit must be 2 or 3
    assert not valid("29013011501234")  # month 13
    assert not valid("29002301501234")  # Feb 30 is not a real date
    assert not valid("29001010501234")  # governorate 05 is unassigned
    assert not valid("29001019901234")  # governorate 99 unknown


def test_employee_national_id_enforced_on_create_and_edit(client):
    import app.main as main_module

    assert login(client).status_code == 303
    token = csrf_from(client.get("/employees"))

    # Empty stays allowed (field optional).
    created = client.post("/employees", data=_employee_form(token), follow_redirects=False)
    assert created.status_code == 303

    # Structurally valid value accepted.
    ok = client.post("/employees", data=_employee_form(token, full_name="Valid ID", national_id="29001011501234"), follow_redirects=False)
    assert ok.status_code == 303

    # Malformed value blocked.
    bad = client.post("/employees", data=_employee_form(token, full_name="Bad ID", national_id="12345"), follow_redirects=False)
    assert bad.status_code == 400
    assert "الرقم القومي" in bad.text

    # Foreign employees are validated by passport, not the Egyptian format.
    foreign = client.post("/employees", data=_employee_form(token, full_name="Foreign Worker", nationality="foreign", passport_number="A123456", national_id="X-1234"), follow_redirects=False)
    assert foreign.status_code == 303

    # Legacy stored value is never revalidated: give an employee a malformed
    # stored ID directly, then edit an unrelated field.
    with main_module.SessionLocal.begin() as db:
        legacy = Employee(
            code="LEG-1", full_name="Legacy ID Employee",
            department_id=client.test_department_id, position="Test Position",
            hire_date=date(2025, 1, 1), basic_salary=1000, daily_hours=8,
            national_id="12345",
        )
        db.add(legacy)
        db.flush()
        legacy_id = legacy.id
    token = csrf_from(client.get("/employees"))
    edit_data = _employee_form(token, full_name="Legacy ID Employee", national_id="12345", effective_date="2025-06-01", reason="phone update")
    edit_data["phone"] = "01099999999"
    keep = client.post(f"/employees/{legacy_id}/edit", data=edit_data, follow_redirects=False)
    assert keep.status_code == 303

    # Changing the stored ID to a malformed value is blocked.
    edit_data["national_id"] = "99999999999999"
    block = client.post(f"/employees/{legacy_id}/edit", data=edit_data, follow_redirects=False)
    assert block.status_code == 400

    # Changing it to a valid value passes.
    edit_data["national_id"] = "30101010123456"
    fix = client.post(f"/employees/{legacy_id}/edit", data=edit_data, follow_redirects=False)
    assert fix.status_code == 303


def test_data_change_request_national_id_validated_on_apply(client):
    import app.main as main_module
    from app.models import EmployeeDataChangeRequest

    assert login(client).status_code == 303
    _add_employee(client, "EMP-301", "Request Employee")
    with main_module.SessionLocal.begin() as db:
        emp = db.query(Employee).filter_by(code="EMP-301").one()
        db.add(EmployeeDataChangeRequest(employee_id=emp.id, field_name="national_id", requested_value="111", request_reason="fix"))
        db.flush()
        bad_request_id = db.query(EmployeeDataChangeRequest).filter_by(employee_id=emp.id).one().id
    token = csrf_from(client.get("/employees"))
    blocked = client.post(
        f"/employee-data-change-requests/{bad_request_id}/review",
        data={"csrf_token": token, "decision": "apply", "decision_reason": "ok", "effective_date": "2025-06-01"},
        follow_redirects=False,
    )
    assert blocked.status_code == 400
    assert "الرقم القومي" in blocked.text
    with main_module.SessionLocal() as db:
        assert db.query(Employee).filter_by(code="EMP-301").one().national_id is None

    with main_module.SessionLocal.begin() as db:
        emp = db.query(Employee).filter_by(code="EMP-301").one()
        db.add(EmployeeDataChangeRequest(employee_id=emp.id, field_name="national_id", requested_value="29001011501234", request_reason="fix"))
        db.flush()
        good_request_id = db.query(EmployeeDataChangeRequest).filter_by(employee_id=emp.id, status="pending").order_by(EmployeeDataChangeRequest.id.desc()).first().id
    applied = client.post(
        f"/employee-data-change-requests/{good_request_id}/review",
        data={"csrf_token": token, "decision": "apply", "decision_reason": "ok", "effective_date": "2025-06-01"},
        follow_redirects=False,
    )
    assert applied.status_code == 303
    with main_module.SessionLocal() as db:
        assert db.query(Employee).filter_by(code="EMP-301").one().national_id == "29001011501234"


def test_security_roster_upload_preview_and_explicit_day_approval(client, monkeypatch, tmp_path):
    from datetime import time
    from io import BytesIO
    from openpyxl import Workbook
    from sqlalchemy import func, select
    from app.models import AuditLog, SecurityShiftAssignment, ShiftPattern

    monkeypatch.setattr("app.main.SECURITY_ROSTER_STAGING_DIR", tmp_path / "staging")
    # The fixture's SessionLocal is replaced with its isolated SQLite database.
    from app.main import SessionLocal
    with SessionLocal() as db:
        department = Department(name="Security", active=True)
        db.add(department)
        db.flush()
        employees = []
        for index, name in enumerate(("Fayez", "Yasser", "Walid"), start=1):
            employee = Employee(
                code=f"SEC-{index}", full_name=name, department_id=department.id,
                position="Security", hire_date=date(2024, 1, 1),
                basic_salary=Decimal("5000"), daily_hours=Decimal("8"), status="active",
            )
            db.add(employee)
            employees.append(employee)
        for code, start, end in (
            ("MIDNIGHT", time(0, 0), time(8, 0)),
            ("MORNING", time(8, 0), time(16, 0)),
            ("EVENING", time(16, 0), time(0, 0)),
        ):
            db.add(ShiftPattern(
                department_id=department.id, code=code, name=code,
                start_time=start, end_time=end, active=True,
                match_before_minutes=60, match_after_minutes=60,
            ))
        db.commit()

    assert login(client).status_code == 303
    page = client.get("/security-roster")
    token = csrf_from(page)
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Date", "Fayez", "Yasser", "Walid"])
    sheet.append([date(2026, 11, 1), "00:00", "08:00", "16:00"])
    sheet.append([date(2026, 11, 2), "00:00", "08:00", "16:00"])
    output = BytesIO()
    workbook.save(output)
    preview = client.post(
        "/security-roster/preview",
        data={"csrf_token": token},
        files={"file": ("security.xlsx", output.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert preview.status_code == 200
    assert "جاهز للاعتماد بعد المراجعة" in preview.text
    stage_match = re.search(r'name="stage_token" value="([^"]+)"', preview.text)
    assert stage_match

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SecurityShiftAssignment)) == 0
        assert db.scalar(select(func.count()).select_from(AuditLog)) == 0

    approved = client.post(
        "/security-roster/confirm",
        data={
            "csrf_token": token,
            "work_date": "2026-11-01",
            "stage_token": stage_match.group(1),
            "reviewed": "yes",
        },
    )
    assert approved.status_code == 200
    assert "تم اعتماد جدول يوم 2026-11-01 بنجاح" in approved.text
    assert "تم اعتماد هذا اليوم بالفعل" in approved.text
    assert 'value="2026-11-02"' in approved.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SecurityShiftAssignment)) == 3
        assert db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "security_roster_day_approved"
        )) == 1

    # The same monthly upload remains available for the next date.
    approved_second_day = client.post(
        "/security-roster/confirm",
        data={
            "csrf_token": token,
            "work_date": "2026-11-02",
            "stage_token": stage_match.group(1),
            "reviewed": "yes",
        },
    )
    assert approved_second_day.status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SecurityShiftAssignment)) == 6
        assert db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "security_roster_day_approved"
        )) == 2



def test_security_roster_upload_preview_and_daily_confirmation(client):
    from datetime import time
    from io import BytesIO

    from openpyxl import Workbook
    from sqlalchemy import select

    from app.main import SessionLocal
    from app.models import AuditLog, SecurityShiftAssignment, ShiftPattern

    assert login(client).status_code == 303

    with SessionLocal.begin() as db:
        department = Department(name="Security", active=True)
        db.add(department)
        db.flush()
        employees = []
        for index, name in enumerate(("Fayez", "Yasser", "Walid"), start=1):
            employee = Employee(
                code=f"SEC-{index}",
                full_name=name,
                fingerprint_id=f"FP-{index}",
                department_id=department.id,
                position="Security",
                hire_date=date(2024, 1, 1),
                basic_salary=Decimal("5000"),
                daily_hours=Decimal("8"),
                status="active",
            )
            db.add(employee)
            employees.append(employee)
        for code, start, end in (
            ("NIGHT", time(0, 0), time(8, 0)),
            ("MORNING", time(8, 0), time(16, 0)),
            ("EVENING", time(16, 0), time(0, 0)),
        ):
            db.add(ShiftPattern(
                department_id=department.id,
                code=code,
                name=code,
                start_time=start,
                end_time=end,
                active=True,
                match_before_minutes=60,
                match_after_minutes=60,
            ))

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Date", "Fayez", "Yasser", "Walid"])
    sheet.append([date(2026, 11, 1), "00:00", "08:00", "16:00"])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()

    page = client.get("/security-roster")
    csrf = csrf_from(page)
    preview = client.post(
        "/security-roster/preview",
        data={"csrf_token": csrf},
        files={
            "file": (
                "security.xlsx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert preview.status_code == 200
    assert "جاهز للاعتماد" in preview.text
    assert "stage_token" in preview.text

    token_match = re.search(r'name="stage_token" value="([^"]+)"', preview.text)
    assert token_match
    stage_token = token_match.group(1)
    assert client.get("/security-roster").status_code == 200

    rejected_without_review = client.post(
        "/security-roster/confirm",
        data={
            "csrf_token": csrf,
            "work_date": "2026-11-01",
            "stage_token": stage_token,
        },
    )
    assert rejected_without_review.status_code == 400

    confirmation = client.post(
        "/security-roster/confirm",
        data={
            "csrf_token": csrf,
            "work_date": "2026-11-01",
            "stage_token": stage_token,
            "reviewed": "yes",
        },
    )
    assert confirmation.status_code == 200
    assert "تم اعتماد جدول يوم 2026-11-01 بنجاح" in confirmation.text

    with SessionLocal() as db:
        assignments = db.scalars(
            select(SecurityShiftAssignment).where(
                SecurityShiftAssignment.work_date == date(2026, 11, 1)
            )
        ).all()
        assert len(assignments) == 3
        audit = db.scalar(select(AuditLog).where(
            AuditLog.action == "security_roster_day_approved"
        ))
        assert audit is not None
        assert audit.actor == "integration-admin"


def test_security_roster_expired_preview_is_rejected_and_deleted(monkeypatch, tmp_path):
    import os
    import time
    from starlette.requests import Request
    from fastapi import HTTPException
    from app import main as main_module

    staging = tmp_path / "staging"
    monkeypatch.setattr(main_module, "SECURITY_ROSTER_STAGING_DIR", staging)
    monkeypatch.setattr(main_module, "SECURITY_ROSTER_STAGING_TTL_SECONDS", 60)
    staging.mkdir()
    token = "expired-preview-token"
    path = staging / f"{token}.xlsx"
    path.write_bytes(b"synthetic workbook")
    old_time = time.time() - 120
    os.utime(path, (old_time, old_time))

    request_scope = {
        "type": "http", "method": "POST", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"",
        "headers": [], "server": ("testserver", 80),
        "client": ("testclient", 50000), "http_version": "1.1",
        "session": {"security_roster_stage": token},
    }
    request = Request(request_scope)

    with pytest.raises(HTTPException) as exc:
        main_module._load_staged_security_roster(token, request)

    assert exc.value.status_code == 410
    assert not path.exists()


def test_security_roster_cleanup_removes_expired_files_without_new_upload(monkeypatch, tmp_path):
    import os
    from app import main as main_module

    staging = tmp_path / "staging"
    monkeypatch.setattr(main_module, "SECURITY_ROSTER_STAGING_DIR", staging)
    monkeypatch.setattr(main_module, "SECURITY_ROSTER_STAGING_TTL_SECONDS", 60)
    staging.mkdir()
    expired = staging / "expired.xlsx"
    fresh = staging / "fresh.xlsx"
    expired.write_bytes(b"old")
    fresh.write_bytes(b"new")
    os.utime(expired, (100, 100))
    os.utime(fresh, (950, 950))

    removed = main_module._cleanup_expired_security_roster_files(now=1000)

    assert removed == 1
    assert not expired.exists()
    assert fresh.exists()


def test_security_roster_preview_token_is_bound_to_its_own_session(monkeypatch, tmp_path):
    from starlette.requests import Request
    from fastapi import HTTPException
    from app import main as main_module

    staging = tmp_path / "staging"
    monkeypatch.setattr(main_module, "SECURITY_ROSTER_STAGING_DIR", staging)
    staging.mkdir()
    token = "admin-one-preview-token"
    (staging / f"{token}.xlsx").write_bytes(b"synthetic workbook")

    second_admin_request = Request({
        "type": "http", "method": "POST", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"",
        "headers": [], "server": ("testserver", 80),
        "client": ("testclient", 50001), "http_version": "1.1",
        "session": {"admin_username": "admin-two", "security_roster_stage": "different-token"},
    })

    with pytest.raises(HTTPException) as exc:
        main_module._load_staged_security_roster(token, second_admin_request)

    assert exc.value.status_code == 403
    assert (staging / f"{token}.xlsx").exists()


def test_security_roster_preview_cannot_cross_admin_sessions_or_survive_logout(client, monkeypatch, tmp_path):
    from datetime import time
    from io import BytesIO
    from openpyxl import Workbook
    from fastapi.testclient import TestClient
    from app.main import SessionLocal
    from app.models import ShiftPattern

    monkeypatch.setattr("app.main.SECURITY_ROSTER_STAGING_DIR", tmp_path / "staging")
    with SessionLocal.begin() as db:
        security = Department(name="Security", active=True)
        db.add(security)
        db.flush()
        for index, name in enumerate(("Fayez", "Yasser", "Walid"), start=1):
            db.add(Employee(
                code=f"SEC-{index}", full_name=name, department_id=security.id,
                position="Security", hire_date=date(2024, 1, 1),
                basic_salary=Decimal("5000"), daily_hours=Decimal("8"), status="active",
            ))
        for code, start, end in (
            ("MIDNIGHT", time(0, 0), time(8, 0)),
            ("MORNING", time(8, 0), time(16, 0)),
            ("EVENING", time(16, 0), time(0, 0)),
        ):
            db.add(ShiftPattern(
                department_id=security.id, code=code, name=code,
                start_time=start, end_time=end, active=True,
                match_before_minutes=60, match_after_minutes=60,
            ))

    assert login(client).status_code == 303
    upload_page = client.get("/security-roster")
    first_csrf = csrf_from(upload_page)
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Date", "Fayez", "Yasser", "Walid"])
    sheet.append([date(2026, 11, 1), "00:00", "08:00", "16:00"])
    output = BytesIO()
    workbook.save(output)
    preview = client.post(
        "/security-roster/preview",
        data={"csrf_token": first_csrf},
        files={"file": ("security.xlsx", output.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert preview.status_code == 200
    stage_match = re.search(r'name="stage_token" value="([^"]+)"', preview.text)
    assert stage_match
    stage_token = stage_match.group(1)

    with TestClient(app) as second_client:
        assert login(second_client).status_code == 303
        second_csrf = csrf_from(second_client.get("/security-roster"))
        cross_session = second_client.post(
            "/security-roster/confirm",
            data={
                "csrf_token": second_csrf,
                "work_date": "2026-11-01",
                "stage_token": stage_token,
                "reviewed": "yes",
            },
        )
        assert cross_session.status_code == 403

    staged_path = tmp_path / "staging" / f"{stage_token}.xlsx"
    assert staged_path.exists()
    logout = client.post(
        "/logout", data={"csrf_token": first_csrf}, follow_redirects=False
    )
    assert logout.status_code == 303
    assert not staged_path.exists()
    assert login(client).status_code == 303
    new_csrf = csrf_from(client.get("/security-roster"))
    after_logout = client.post(
        "/security-roster/confirm",
        data={
            "csrf_token": new_csrf,
            "work_date": "2026-11-01",
            "stage_token": stage_token,
            "reviewed": "yes",
        },
    )
    assert after_logout.status_code == 403


def test_attendance_import_route_saves_raw_evidence_but_not_daily_approval(client):
    from datetime import time
    from io import BytesIO
    from openpyxl import Workbook
    from app.main import SessionLocal
    from app.models import AttendanceDailyRecord, AttendanceImportBatch, AttendancePunch

    with SessionLocal.begin() as db:
        db.add(Employee(
            code="ATT-ROUTE-01", full_name="Attendance Test Employee",
            department_id=client.test_department_id, position="Test",
            hire_date=date(2024, 1, 1), status="active",
            basic_salary=Decimal("5000"), daily_hours=Decimal("8"),
            fingerprint_id="FP-ROUTE-01",
        ))

    assert login(client).status_code == 303
    page = client.get("/attendance/import")
    token = csrf_from(page)
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["AC-No.", "Name", "Date", "Clock In", "Clock Out"])
    sheet.append(["ATT-ROUTE-01", "Attendance Test Employee", date(2026, 11, 1), time(8, 0), time(16, 0)])
    output = BytesIO()
    workbook.save(output)

    response = client.post(
        "/attendance/import",
        data={"csrf_token": token, "source_type": "program_report"},
        files={"file": ("program.xlsX", output.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 200
    assert "تم تسجيل دفعة الاستيراد" in response.text
    with SessionLocal() as db:
        batch = db.query(AttendanceImportBatch).one()
        assert batch.original_file_bytes == output.getvalue()
        batch_id = batch.id
        assert db.query(AttendancePunch).count() == 2
        assert db.query(AttendanceDailyRecord).count() == 0

    download = client.get(f"/attendance/import/{batch_id}/download")
    assert download.status_code == 200
    assert download.content == output.getvalue()
    assert "attachment" in download.headers["content-disposition"]


def test_attendance_unknown_identity_can_be_resolved_only_by_authenticated_admin(client):
    from datetime import datetime
    from app.main import SessionLocal
    from app.models import (
        AttendanceImportBatch, AttendanceImportIssue, AttendancePunch, AuditLog,
    )

    with SessionLocal.begin() as db:
        employee = Employee(
            code="ATT-IDENTITY-01", full_name="Identity Target",
            department_id=client.test_department_id, position="Test",
            hire_date=date(2024, 1, 1), status="active",
            basic_salary=Decimal("5000"), daily_hours=Decimal("8"),
        )
        db.add(employee)
        db.flush()
        batch = AttendanceImportBatch(
            source_type="fingerprint", period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 1), original_filename="fingerprint.xlsx",
            file_sha256="a" * 64, original_file_bytes=b"source-file",
            uploaded_by="integration-admin", status="needs_review",
            row_count=1, issue_count=1,
        )
        db.add(batch)
        db.flush()
        issue = AttendanceImportIssue(
            import_batch_id=batch.id, source_sheet="Att.log report",
            source_row=8, reason="unknown_employee", raw_value="FP-UNKNOWN",
        )
        db.add(issue)
        db.flush()
        db.add(AttendancePunch(
            import_batch_id=batch.id, employee_id=None,
            raw_employee_code="FP-UNKNOWN", raw_employee_name="Unknown",
            punch_at=datetime(2026, 11, 1, 7, 50), raw_timestamp="07:50",
            event_type="PUNCH", source_sheet="Att.log report",
            source_row=8, event_index=0,
        ))
        batch_id, issue_id, employee_id = batch.id, issue.id, employee.id

    assert login(client).status_code == 303
    page = client.get(f"/attendance/import/{batch_id}/issues")
    assert page.status_code == 200
    token = csrf_from(page)
    assert "FP-UNKNOWN" in page.text

    response = client.post(
        f"/attendance/import/{batch_id}/issues/{issue_id}/resolve",
        data={"csrf_token": token, "employee_id": str(employee_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with SessionLocal() as db:
        batch = db.get(AttendanceImportBatch, batch_id)
        issue = db.get(AttendanceImportIssue, issue_id)
        punch = db.query(AttendancePunch).filter_by(import_batch_id=batch_id).one()
        assert batch.status == "parsed"
        assert batch.issue_count == 0
        assert issue.resolved_employee_id == employee_id
        assert punch.employee_id == employee_id
        assert punch.raw_employee_code == "FP-UNKNOWN"
        assert db.query(AuditLog).filter_by(
            action="attendance_import_unknown_employee_resolved"
        ).count() == 1


def test_attendance_month_page_displays_unrepresented_employee_day_gaps(client):
    from app.main import SessionLocal
    from app.models import AttendanceImportBatch, AttendanceMonth

    with SessionLocal.begin() as db:
        employee = Employee(
            code="ATT-COVERAGE-ROUTE", full_name="Coverage Route Employee",
            department_id=client.test_department_id, position="Test",
            hire_date=date(2024, 1, 1), status="active",
            basic_salary=Decimal("5000"), daily_hours=Decimal("8"),
        )
        db.add(employee)
        db.flush()
        program = AttendanceImportBatch(
            source_type="program_report", period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 1), original_filename="program.xlsx",
            file_sha256="3" * 64, uploaded_by="integration-admin",
            status="parsed", row_count=0, issue_count=0,
        )
        fingerprint = AttendanceImportBatch(
            source_type="fingerprint", period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 1), original_filename="fingerprint.xlsx",
            file_sha256="4" * 64, uploaded_by="integration-admin",
            status="parsed", row_count=0, issue_count=0,
        )
        db.add_all([program, fingerprint])
        db.flush()
        db.add(AttendanceMonth(
            period_start=date(2026, 11, 1), period_end=date(2026, 11, 1),
            program_import_batch_id=program.id,
            fingerprint_import_batch_id=fingerprint.id,
            status="under_review", created_by="integration-admin",
        ))

    assert login(client).status_code == 303
    response = client.get("/attendance/months")
    assert response.status_code == 200
    assert "أيام موظفين بلا سجل مطابقة: 1" in response.text
    assert "Coverage Route Employee" in response.text
    assert "لا يصنفها النظام غيابًا أو راحة تلقائيًا" in response.text


def test_attendance_import_route_requires_admin_and_csrf(client):
    assert client.get("/attendance/import").status_code == 401
    assert client.get("/attendance/import/1/download").status_code == 401
    assert client.get("/attendance/import/1/issues").status_code == 401
    response = client.post("/attendance/import", data={"source_type": "fingerprint"})
    assert response.status_code == 403
    resolution = client.post(
        "/attendance/import/1/issues/1/resolve", data={"employee_id": "1"}
    )
    assert resolution.status_code == 403


def test_monthly_attendance_reconciliation_page_is_admin_protected(client):
    assert client.get("/attendance/reconcile").status_code == 401
    assert login(client).status_code == 303
    page = client.get("/attendance/reconcile")
    assert page.status_code == 200
    assert "مطابقة الحضور الشهرية" in page.text
    assert client.post("/attendance/reconcile", data={}).status_code == 403


def test_attendance_review_page_is_admin_protected_and_renders_empty_state(client):
    assert client.get("/attendance/review").status_code == 401
    assert login(client).status_code == 303
    page = client.get("/attendance/review")
    assert page.status_code == 200
    assert "مراجعة واعتماد الحضور" in page.text
    assert "لا توجد سجلات معلقة" in page.text


def test_attendance_approval_endpoint_requires_csrf_and_admin(client):
    assert client.post(
        "/attendance/1/approve",
        data={"clock_in": "2026-11-01T08:00", "clock_out": "2026-11-01T16:00", "reason": "reviewed"},
    ).status_code == 403
    assert login(client).status_code == 303
    response = client.post(
        "/attendance/1/approve",
        data={"clock_in": "2026-11-01T08:00", "clock_out": "2026-11-01T16:00", "reason": "reviewed"},
    )
    assert response.status_code == 403


def test_overnight_attendance_review_prefills_clock_out_on_next_date(client):
    from datetime import datetime, time
    from app.main import SessionLocal
    from app.models import AttendanceDailyRecord, ShiftPattern

    with SessionLocal.begin() as db:
        employee = Employee(
            code="ATT-NIGHT-01", full_name="Night Shift Employee",
            department_id=client.test_department_id, position="Test",
            hire_date=date(2024, 1, 1), status="active",
            basic_salary=Decimal("5000"), daily_hours=Decimal("8"),
        )
        db.add(employee)
        db.flush()
        shift = ShiftPattern(
            department_id=client.test_department_id, code="NIGHT-TEST",
            name="Night Test", start_time=time(23, 0), end_time=time(7, 0),
            match_before_minutes=60, match_after_minutes=60, active=True,
        )
        db.add(shift)
        db.flush()
        db.add(AttendanceDailyRecord(
            employee_id=employee.id, work_date=date(2026, 11, 1),
            shift_pattern_id=shift.id,
            scheduled_start_at=datetime(2026, 11, 1, 23, 0),
            scheduled_end_at=datetime(2026, 11, 2, 7, 0),
            program_clock_in=datetime(2026, 11, 1, 23, 5),
            program_clock_out=datetime(2026, 11, 1, 7, 3),
            first_clock_in=datetime(2026, 11, 1, 23, 5),
            last_clock_out=datetime(2026, 11, 2, 7, 3),
            worked_minutes=478, status="ready_for_review",
        ))

    assert login(client).status_code == 303
    page = client.get("/attendance/review")
    assert page.status_code == 200
    assert 'value="2026-11-02T07:03"' in page.text


def test_language_switch_persists_and_renders_chinese_ltr(client):
    response = client.get("/language/zh", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"].lower()
    assert "my_hr_language=zh" in cookie
    assert "max-age=31536000" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie

    page = client.get("/login")
    assert page.status_code == 200
    assert page.headers["content-language"] == "zh-CN"
    assert '<html lang="zh-CN" dir="ltr">' in page.text
    assert "登录" in page.text


def test_language_switch_supports_english_and_arabic_default(client):
    english = client.get("/language/en", follow_redirects=False)
    assert english.status_code == 303
    page = client.get("/login")
    assert page.headers["content-language"] == "en"
    assert '<html lang="en" dir="ltr">' in page.text
    assert "Sign In" in page.text

    client.cookies.delete("my_hr_language")
    arabic = client.get("/login")
    assert arabic.headers["content-language"] == "ar"
    assert '<html lang="ar" dir="rtl">' in arabic.text
    assert "تسجيل الدخول" in arabic.text


def test_language_switch_rejects_unsupported_locale(client):
    response = client.get("/language/fr", follow_redirects=False)
    assert response.status_code == 404


def test_login_post_and_csrf_continue_to_work_after_language_switch(client):
    client.get("/language/en", follow_redirects=False)
    page = client.get("/login")
    assert page.headers["content-language"] == "en"
    token = csrf_from(page)
    response = client.post(
        "/login",
        data={
            "username": "integration-admin",
            "password": "Integration-Test-Password-123!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert dashboard.headers["content-language"] == "en"
    assert "Dashboard" in dashboard.text


def test_language_switch_keeps_safe_same_origin_page_and_query(client):
    response = client.get(
        "/language/zh",
        headers={"referer": "http://testserver/login?from=language"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?from=language"



@pytest.mark.parametrize(
    ("language", "html_lang", "direction", "content_language", "navigation_label"),
    [
        ("ar", "ar", "rtl", "ar", "لوحة التحكم"),
        ("en", "en", "ltr", "en", "Dashboard"),
        ("zh", "zh-CN", "ltr", "zh-CN", "控制面板"),
    ],
)
def test_all_main_html_pages_render_in_each_supported_language(
    client, language, html_lang, direction, content_language, navigation_label
):
    assert login(client).status_code == 303
    client.cookies.set("my_hr_language", language)

    paths = [
        "/", "/ai", "/security-roster", "/attendance/import",
        "/attendance/reconcile", "/attendance/review", "/attendance/months",
        "/leave", "/advances", "/staff-actions", "/payroll", "/reports",
        "/settings/backup", "/employees",
    ]
    for path in paths:
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 200, f"{language} {path}: {response.status_code}"
        assert response.headers["content-language"] == content_language
        assert f'<html lang="{html_lang}" dir="{direction}">' in response.text
        assert navigation_label in response.text, f"Missing translated navigation on {path}"
        if path == "/" and language != "ar":
            expected_greeting = "Welcome," if language == "en" else "欢迎，"
            assert expected_greeting in response.text
        if language != "ar":
            probe = _ArabicUiTextProbe()
            probe.feed(response.text)
            assert not probe.arabic_parts, (
                f"Untranslated Arabic UI content for {language} on {path}: "
                f"{probe.arabic_parts[:8]}"
            )
        assert 'href="/settings/backup"' in response.text
        assert 'href="/backup"' not in response.text


def test_ai_pending_confirmation_uses_v22_routes_and_shows_before_after(monkeypatch, client):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    assert login(client).status_code == 303
    monkeypatch.setattr("app.main.is_command_request", lambda question: True)
    monkeypatch.setattr(
        "app.main.prepare_command",
        lambda question, actor: {
            "token": "single-use-test-token",
            "summary": "تحديث تجريبي",
            "plan": {"action": "edit_employee", "employee_id": 7},
            "old_values": {"الراتب": "5000"},
            "new_values": {"الراتب": "5500"},
            "risks": ["مراجعة الأثر المالي"],
            "expires_at": datetime.now(timezone.utc),
        },
    )
    monkeypatch.setattr(
        "app.main.local_model_status",
        lambda language="ar": SimpleNamespace(available=False, message="النموذج غير متاح", model="test-model"),
    )
    page = client.get("/ai")
    response = client.post(
        "/ai",
        data={"csrf_token": csrf_from(page), "question": "اعرض خطة تعديل تجريبية"},
    )
    assert response.status_code == 200
    assert "البيانات الحالية قبل التنفيذ" in response.text
    assert "التغييرات المطلوبة بعد التنفيذ" in response.text
    assert "5000" in response.text and "5500" in response.text
    assert 'action="/ai/approve"' in response.text
    assert 'action="/ai/reject"' in response.text
    assert 'name="token" value="single-use-test-token"' in response.text
    assert 'action="/ai/prepare"' not in response.text
    assert 'action="/ai/confirm"' not in response.text


def test_ai_question_form_preserves_question_and_uses_existing_post_route(monkeypatch, client):
    from types import SimpleNamespace

    assert login(client).status_code == 303
    monkeypatch.setattr("app.main.is_command_request", lambda question: False)
    monkeypatch.setattr("app.main.is_structured_hr_query", lambda question: False)
    monkeypatch.setattr("app.main.ask_local_model", lambda question, context, language="ar": "إجابة تجريبية")
    monkeypatch.setattr(
        "app.main.local_model_status",
        lambda language="ar": SimpleNamespace(available=True, message="جاهز", model="test-model"),
    )
    page = client.get("/ai")
    response = client.post(
        "/ai",
        data={"csrf_token": csrf_from(page), "question": "اشرح ملخص الحضور"},
    )
    assert response.status_code == 200
    assert 'action="/ai"' in response.text
    assert "اشرح ملخص الحضور" in response.text
    assert "إجابة تجريبية" in response.text


def _add_employee(client, code, name, status="active", phone=None, national_id=None, department_id=None):
    import app.main as main_module

    with main_module.SessionLocal() as db:
        db.add(Employee(
            code=code,
            full_name=name,
            department_id=department_id or client.test_department_id,
            position="Test Position",
            hire_date=date(2025, 1, 1),
            basic_salary=1000,
            daily_hours=8,
            status=status,
            phone=phone,
            national_id=national_id,
        ))
        db.commit()


def test_employees_list_search_filters_and_pagination(client, monkeypatch):
    import app.main as main_module

    assert login(client).status_code == 303
    _add_employee(client, "EMP-101", "Ahmed Ali", phone="01001234567", national_id="30001010101010")
    _add_employee(client, "EMP-102", "Mona Said")
    _add_employee(client, "EMP-103", "Retired Worker", status="archived")

    page = client.get("/employees")
    assert "Ahmed Ali" in page.text and "Mona Said" in page.text and "Retired Worker" in page.text

    by_name = client.get("/employees", params={"q": "Ahmed"})
    assert "Ahmed Ali" in by_name.text and "Mona Said" not in by_name.text

    by_phone = client.get("/employees", params={"q": "0100123"})
    assert "Ahmed Ali" in by_phone.text and "Mona Said" not in by_phone.text

    by_code = client.get("/employees", params={"q": "EMP-102"})
    assert "Mona Said" in by_code.text and "Ahmed Ali" not in by_code.text

    by_national_id = client.get("/employees", params={"q": "3000101"})
    assert "Ahmed Ali" in by_national_id.text and "Mona Said" not in by_national_id.text

    archived_only = client.get("/employees", params={"status": "archived"})
    assert "Retired Worker" in archived_only.text
    assert "Ahmed Ali" not in archived_only.text

    no_hits = client.get("/employees", params={"q": "no-such-person"})
    assert "لا توجد نتائج مطابقة للبحث أو الفلاتر الحالية." in no_hits.text

    monkeypatch.setattr(main_module, "EMPLOYEES_PER_PAGE", 2)
    first = client.get("/employees", params={"q": "e"})
    assert "Ahmed Ali" in first.text and "Mona Said" in first.text
    assert "Retired Worker" not in first.text
    assert "صفحة" in first.text and "التالي" in first.text and "page=2" in first.text
    second = client.get("/employees", params={"q": "e", "page": 2})
    assert "Retired Worker" in second.text
    assert "Ahmed Ali" not in second.text
    assert "السابق" in second.text and "page=1" in second.text
    # The search term survives pagination links.
    assert "q=e" in first.text and "q=e" in second.text

    # Out-of-range pages clamp instead of erroring.
    clamped = client.get("/employees", params={"page": 99})
    assert clamped.status_code == 200

    # The new states are translated, not Arabic leftovers, on other languages.
    client.cookies.set("my_hr_language", "en")
    en_empty = client.get("/employees", params={"q": "no-such-person"})
    assert "No results match the current search or filters." in en_empty.text
    en_page = client.get("/employees", params={"q": "e"})
    assert "Next" in en_page.text and "Page" in en_page.text and "Search" in en_page.text
    client.cookies.set("my_hr_language", "zh")
    zh_page = client.get("/employees", params={"q": "e"})
    assert "下一页" in zh_page.text and "搜索" in zh_page.text


def test_employees_page_bounds_change_and_correction_records(client, monkeypatch):
    """Change history is scoped to visible cards; correction requests paginate fully."""
    import app.main as main_module
    from app.models import EmployeeChange, EmployeeDataChangeRequest

    assert login(client).status_code == 303
    _add_employee(client, "EMP-201", "Visible Employee")
    with main_module.SessionLocal() as db:
        visible = db.query(Employee).filter_by(code="EMP-201").one()
        other = Employee(
            code="EMP-202",
            full_name="Offpage Employee",
            department_id=client.test_department_id,
            position="Test Position",
            hire_date=date(2025, 1, 1),
            basic_salary=1000,
            daily_hours=8,
        )
        db.add(other)
        db.flush()
        # A change record for an employee outside the current page must not be
        # loaded at all; one for a visible employee must still render.
        db.add(EmployeeChange(employee_id=other.id, field_name="phone", old_value="1", new_value="2", reason="offpage", effective_date=date(2025, 2, 1)))
        db.add(EmployeeChange(employee_id=visible.id, field_name="phone", old_value="3", new_value="4", reason="visible-reason", effective_date=date(2025, 2, 2)))
        db.add(EmployeeDataChangeRequest(employee_id=visible.id, field_name="address", requested_value="oldest requested value", request_reason="r1", recorded_at=datetime(2025, 2, 1)))
        db.add(EmployeeDataChangeRequest(employee_id=visible.id, field_name="phone", requested_value="middle requested value", request_reason="r2", recorded_at=datetime(2025, 2, 2)))
        db.add(EmployeeDataChangeRequest(employee_id=visible.id, field_name="phone", requested_value="newest requested value", request_reason="r3", recorded_at=datetime(2025, 2, 3)))
        db.commit()

    monkeypatch.setattr(main_module, "DATA_CHANGE_REQUESTS_PER_PAGE", 2)
    first = client.get("/employees", params={"q": "EMP-201"})
    assert first.status_code == 200
    assert "visible-reason" in first.text
    assert "offpage" not in first.text
    assert "newest requested value" in first.text
    assert "middle requested value" in first.text
    assert "oldest requested value" not in first.text
    # Every request stays reachable: the pager links to the next requests page
    # and preserves the active employee filters.
    assert "req_page=2" in first.text and "q=EMP-201" in first.text

    second = client.get("/employees", params={"q": "EMP-201", "req_page": 2})
    assert "oldest requested value" in second.text
    assert "newest requested value" not in second.text
    assert "req_page=1" in second.text

    # The employee-card pager keeps the requests page in its links.
    monkeypatch.setattr(main_module, "EMPLOYEES_PER_PAGE", 1)
    cards = client.get("/employees", params={"req_page": 2})
    assert "req_page=2" in cards.text

    client.cookies.set("my_hr_language", "en")
    en_page = client.get("/employees", params={"q": "EMP-201"})
    assert "Next" in en_page.text
    client.cookies.set("my_hr_language", "zh")
    zh_page = client.get("/employees", params={"q": "EMP-201"})
    assert "下一页" in zh_page.text
