"""Employees page: list-first layout with the add form in a modal (round-2 UI).

The rendered add form must keep every field and actually submit when driven
by its own parsed action/inputs; the modal toggle is system-admin-only; the
same payload without CSRF is rejected.
"""
import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, Branch, Department, Employee, EmployeeCodeCounter, LeaveType
from app.security import hash_password

ADMIN_PW = "Emp-Modal-Admin-Pass-123!"
FINANCE_PW = "Emp-Modal-Finance-Pass-123!"

ADD_FIELDS = {
    "full_name", "nationality", "national_id", "passport_number", "phone",
    "address", "department_id", "branch_id", "position", "hire_date",
    "probation_start_date", "probation_end_date", "basic_salary",
    "daily_hours", "fingerprint_id", "photo",
}


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._current = {"action": attrs.get("action", ""), "method": attrs.get("method", "get").lower(), "inputs": {}}
        elif tag in ("input", "select", "textarea") and self._current is not None:
            name = attrs.get("name")
            if name:
                self._current["inputs"][name] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


@pytest.fixture()
def emp_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        dept = Department(name="قسم الموظفين", active=True)
        db.add(dept)
        db.flush()
        db.add_all([
            AdminAccount(username="emp-admin", password_hash=hash_password(ADMIN_PW),
                         display_name="مدير الموظفين", role="system_admin", active=True),
            AdminAccount(username="emp-finance", password_hash=hash_password(FINANCE_PW),
                         display_name="مالي", role="finance", active=True),
            Branch(name="فرع أساسي", active=True, department_id=dept.id),
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


def _login(client, username, password):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _add_form(response):
    parser = _FormParser()
    parser.feed(response.text)
    for form in parser.forms:
        if form["action"] == "/employees" and form["method"] == "post":
            return form
    raise AssertionError(f"no rendered POST form for /employees: {[(f['method'], f['action']) for f in parser.forms]}")


def test_add_form_lives_in_modal_with_all_fields(emp_client):
    client = emp_client
    _login(client, "emp-admin", ADMIN_PW)
    page = client.get("/employees")
    assert page.status_code == 200
    assert 'id="add-employee-toggle"' in page.text, "checkbox toggle renders"
    assert 'for="add-employee-toggle"' in page.text, "open button renders"
    assert "إضافة موظف" in page.text
    assert "employee-modal" in page.text, "modal container renders"
    form = _add_form(page)
    missing = ADD_FIELDS - set(form["inputs"])
    assert not missing, f"rendered add form lost fields: {missing}"
    assert form["inputs"].get("csrf_token"), "csrf hidden input renders"


def test_rendered_add_form_submits(emp_client):
    client = emp_client
    _login(client, "emp-admin", ADMIN_PW)
    page = client.get("/employees")
    form = _add_form(page)
    payload = {k: v for k, v in form["inputs"].items()}
    payload.update({
        "full_name": "موظف عبر النموذج",
        "nationality": "egyptian",
        "department_id": "1",
        "branch_id": "1",
        "position": "استقبال",
        "hire_date": "2026-01-15",
        "basic_salary": "5500",
        "daily_hours": "8",
    })
    response = client.post(form["action"], data=payload, follow_redirects=False)
    assert response.status_code == 303, response.text[:300]
    with client.test_session_factory() as db:
        created = db.scalar(select(Employee).where(Employee.full_name == "موظف عبر النموذج"))
        assert created is not None
        assert created.position == "استقبال"


def test_modal_hidden_from_finance_and_post_forbidden(emp_client):
    client = emp_client
    _login(client, "emp-finance", FINANCE_PW)
    page = client.get("/employees")
    assert page.status_code == 200
    assert "add-employee-toggle" not in page.text, "modal toggle must not render for finance"
    response = client.post(
        "/employees",
        data={"csrf_token": "forged", "full_name": "محاولة", "department_id": "1",
              "position": "x", "hire_date": "2026-01-01", "basic_salary": "1",
              "daily_hours": "8"},
        follow_redirects=False,
    )
    assert response.status_code in (400, 403)


def test_add_form_without_csrf_rejected(emp_client):
    client = emp_client
    _login(client, "emp-admin", ADMIN_PW)
    page = client.get("/employees")
    form = _add_form(page)
    payload = {k: v for k, v in form["inputs"].items() if k != "csrf_token"}
    payload.update({
        "full_name": "بدون رمز",
        "department_id": "1",
        "position": "استقبال",
        "hire_date": "2026-01-15",
        "basic_salary": "5500",
        "daily_hours": "8",
    })
    response = client.post(form["action"], data=payload, follow_redirects=False)
    assert response.status_code in (400, 403)
    with client.test_session_factory() as db:
        assert db.scalar(select(Employee).where(Employee.full_name == "بدون رمز")) is None
