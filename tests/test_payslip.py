"""Per-employee payslip PDF: builder output and route access."""
import re
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, Department, Employee, PayrollItem, PayrollRun
from app.payslip import build_payslip_pdf
from app.security import hash_password

PASSWORD = "Payslip-Test-Password-123!"


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with session_factory() as db:
        db.add(AdminAccount(username="payslip-admin", password_hash=hash_password(PASSWORD), active=True))
        department = Department(name="الاستقبال", active=True)
        db.add(department)
        db.flush()
        employee = Employee(
            code="MY-0001", full_name="موظف تجريبي", department_id=department.id,
            position="موظف استقبال", hire_date=date(2026, 1, 1),
            basic_salary=Decimal("6000.00"), daily_hours=Decimal("8.00"), status="active",
        )
        db.add(employee)
        db.flush()
        run = PayrollRun(
            period_start=date(2026, 11, 1), period_end=date(2026, 11, 30),
            pay_date=date(2026, 12, 1), status="approved", created_by="payslip-admin",
        )
        db.add(run)
        db.flush()
        db.add(PayrollItem(
            payroll_run_id=run.id, employee_id=employee.id,
            employee_code_snapshot="MY-0001", employee_name_snapshot="موظف تجريبي",
            department_name_snapshot="الاستقبال", position_snapshot="موظف استقبال",
            salary_basis=Decimal("6000.00"), base_salary=Decimal("6000.00"),
            daily_hours=Decimal("8.00"),
            attendance_bonus=Decimal("300.00"), overtime_hours=Decimal("4.00"),
            overtime_amount=Decimal("100.00"), absence_days=Decimal("1.00"),
            absence_deduction=Decimal("200.00"), insurance_share=Decimal("150.00"),
            net_pay=Decimal("6050.00"),
        ))
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


def login(client):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": "payslip-admin", "password": PASSWORD, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def load_run_item(factory):
    from sqlalchemy import select
    with factory() as db:
        run = db.scalar(select(PayrollRun))
        item = db.scalar(select(PayrollItem))
        return run, item


def test_payslip_pdf_builds_in_all_three_languages(factory):
    run, item = load_run_item(factory)
    now = datetime.now(timezone.utc)
    for language in ("ar", "en", "zh"):
        pdf = build_payslip_pdf(run, item, (Decimal("0"), item.net_pay, "unpaid"), now, language=language)
        assert pdf.startswith(b"%PDF"), language
        assert len(pdf) > 2000, language


def test_payslip_route_requires_admin(client):
    response = client.get("/payroll/runs/1/items/1/payslip.pdf")
    assert response.status_code == 401


def test_payslip_route_serves_pdf(client):
    login(client)
    response = client.get("/payroll/runs/1/items/1/payslip.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert "payslip-MY-0001" in response.headers["content-disposition"]


def test_payslip_route_404_on_mismatch(client):
    login(client)
    assert client.get("/payroll/runs/1/items/999/payslip.pdf").status_code == 404
    assert client.get("/payroll/runs/999/items/1/payslip.pdf").status_code == 404


def test_run_page_links_payslip(client):
    login(client)
    page = client.get("/payroll/runs/1")
    assert page.status_code == 200
    assert "/payroll/runs/1/items/1/payslip.pdf" in page.text
