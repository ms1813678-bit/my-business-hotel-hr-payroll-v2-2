"""Server-side pagination on the leave, advances and staff-action lists.

The record tables page at 100 rows while the on-page totals (leave
balances, the per-employee advance summary) keep covering every record.
"""
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    AdminAccount, AdvanceRequest, Department, Employee, LeaveRecord, LeaveType,
    StaffAction,
)
from app.security import hash_password

PASSWORD = "Pagination-Test-Password-123!"


@pytest.fixture()
def page_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    today = date.today()
    with factory() as db:
        admin = AdminAccount(
            username="pagination-admin",
            password_hash=hash_password(PASSWORD),
            active=True,
        )
        department = Department(name="Pagination Department", active=True)
        db.add_all([admin, department])
        db.flush()
        employee = Employee(
            code="MY-PAGE-0001", full_name="Synthetic Pagination Employee",
            department_id=department.id, position="Test", hire_date=today - timedelta(days=100),
            status="active", basic_salary=Decimal("5000.00"), daily_hours=Decimal("8"),
        )
        db.add(employee)
        leave_type = LeaveType(code="ann", name_ar="سنوية", name_en="Annual", tracks_balance=True)
        db.add(leave_type)
        db.flush()
        start = today - timedelta(days=400)
        db.add_all([
            LeaveRecord(
                employee_id=employee.id, leave_type_id=leave_type.id,
                start_date=start + timedelta(days=i), end_date=start + timedelta(days=i),
                calendar_days=1, charged_days=Decimal("1"),
                reason=f"سبب اختبار رقم {i}", status="draft", created_by="seed",
            )
            for i in range(101)
        ])
        db.add_all([
            AdvanceRequest(
                employee_id=employee.id, request_date=today - timedelta(days=i + 1),
                payroll_month=today.replace(day=1), amount=Decimal("100.00"),
                salary_basis=Decimal("5000.00"), reason=f"سبب سلفة رقم {i}",
                status="pending", created_by="seed",
            )
            for i in range(101)
        ])
        db.add_all([
            StaffAction(
                employee_id=employee.id, kind="sanction", category="deduction",
                title=f"إجراء رقم {i}", is_financial=False, reason="سبب تجريبي",
                status="draft", created_by="seed",
            )
            for i in range(101)
        ])
        db.commit()
        employee_id = employee.id

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", factory)
    try:
        with TestClient(app) as client:
            client.test_employee_id = employee_id
            yield client
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
        data={"username": "pagination-admin", "password": PASSWORD,
              "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_leave_records_paginate_and_keep_full_balances(page_client):
    login(page_client)
    first = page_client.get("/leave")
    assert first.status_code == 200
    assert re.search(r"صفحة</span>\s*1 / 2", first.text)
    assert "100 / 101" in first.text
    assert first.text.count("سبب اختبار رقم") == 100
    second = page_client.get("/leave", params={"page": 2})
    assert re.search(r"صفحة</span>\s*2 / 2", second.text)
    assert second.text.count("سبب اختبار رقم") == 1
    # out-of-range pages clamp to the last page
    clamped = page_client.get("/leave", params={"page": 99})
    assert re.search(r"صفحة</span>\s*2 / 2", clamped.text)
    # the balances section still renders from the whole ledger
    assert "أرصدة الموظفين الحالية" in second.text


def test_advances_table_paginates_and_summary_covers_everything(page_client):
    login(page_client)
    first = page_client.get("/advances")
    assert first.status_code == 200
    assert re.search(r"صفحة</span>\s*1 / 2", first.text)
    assert first.text.count("سبب سلفة رقم") == 100
    second = page_client.get("/advances", params={"page": 2})
    assert second.text.count("سبب سلفة رقم") == 1
    # the summary counts all 101 requests of 100.00 on every page
    assert "10100.00" in first.text
    assert "10100.00" in second.text


def test_staff_actions_paginate(page_client):
    login(page_client)
    first = page_client.get("/staff-actions")
    assert first.status_code == 200
    assert re.search(r"صفحة</span>\s*1 / 2", first.text)
    assert first.text.count("إجراء رقم") == 100
    second = page_client.get("/staff-actions", params={"page": 2})
    assert re.search(r"صفحة</span>\s*2 / 2", second.text)
    assert second.text.count("إجراء رقم") == 1
