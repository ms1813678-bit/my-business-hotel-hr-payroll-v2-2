"""Approved payroll guards: absence/paid-leave conflict review + stale snapshot warning."""
import re
from datetime import date, datetime
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
    AttendanceImportBatch,
    AttendanceMonth,
    Department,
    Employee,
    EmployeeChange,
    EmployeeCodeCounter,
    EmployeeServicePeriod,
    LeaveRecord,
    LeaveType,
    PayrollItem,
    PayrollRun,
)
from app.security import hash_password

PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 10, 31)


@pytest.fixture()
def guard_client(monkeypatch):
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
                password_hash=hash_password("Guard-Test-Password-123!"),
                active=True,
            ),
            Department(name="Guard Test Department", active=True),
            EmployeeCodeCounter(id=1, last_number=0),
            LeaveType(code="ANNUAL", name_ar="سنوية", name_en="Annual", is_paid=True),
            LeaveType(code="UNPAID", name_ar="بدون أجر", name_en="Unpaid", is_paid=False),
        ])
        db.flush()
        employee = Employee(
            code="MY-TEST-GRD-0001",
            full_name="Synthetic Guard Employee",
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
        program = AttendanceImportBatch(
            source_type="program_report",
            period_start=PERIOD_START, period_end=PERIOD_END,
            original_filename="synthetic-program.xlsx",
            file_sha256="a" * 64, uploaded_by="guard-admin",
            status="approved", row_count=1, issue_count=0,
        )
        fingerprint = AttendanceImportBatch(
            source_type="fingerprint",
            period_start=PERIOD_START, period_end=PERIOD_END,
            original_filename="synthetic-fingerprint.xlsx",
            file_sha256="b" * 64, uploaded_by="guard-admin",
            status="approved", row_count=1, issue_count=0,
        )
        db.add_all([program, fingerprint])
        db.flush()
        db.add(AttendanceMonth(
            period_start=PERIOD_START, period_end=PERIOD_END,
            program_import_batch_id=program.id,
            fingerprint_import_batch_id=fingerprint.id,
            status="approved", created_by="guard-admin",
            approved_by="guard-admin", approved_at=datetime.utcnow(),
            approval_reason="Synthetic approved attendance",
        ))
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


def login(client):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": "guard-admin",
            "password": "Guard-Test-Password-123!",
            "csrf_token": csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def create_run(client):
    token = csrf(client.get("/payroll"))
    response = client.post(
        "/payroll",
        data={"csrf_token": token, "year": 2026, "month": 10},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with client.test_session_factory() as db:
        run = db.scalar(select(PayrollRun))
        item = db.scalar(select(PayrollItem))
        return run.id, item.id


def set_absence(client, item_id, days):
    page = client.get(f"/payroll/runs/{client.test_run_id}")
    token = csrf(page)
    response = client.post(
        f"/payroll/items/{item_id}/update",
        data={
            "csrf_token": token,
            "attendance_bonus": "0", "performance_bonus": "0", "occupancy_bonus": "0",
            "overtime_hours": "0", "other_bonus": "0", "absence_days": str(days),
            "lateness_deduction": "0", "insurance_share": "0", "taxes": "0",
            "other_deductions": "0", "note": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error" not in response.headers["location"]


def add_leave(client, leave_type_code, status):
    with client.test_session_factory() as db:
        leave_type = db.scalar(select(LeaveType).where(LeaveType.code == leave_type_code))
        db.add(LeaveRecord(
            employee_id=client.test_employee_id,
            leave_type_id=leave_type.id,
            start_date=date(2026, 10, 10), end_date=date(2026, 10, 12),
            calendar_days=3, charged_days=Decimal("3"),
            reason="Synthetic leave", status=status, created_by="guard-admin",
        ))
        db.commit()


def approve_run(client, run_id):
    page = client.get(f"/payroll/runs/{run_id}")
    token = csrf(page)
    return client.post(
        f"/payroll/runs/{run_id}/approve",
        data={"csrf_token": token},
        follow_redirects=False,
    )


def test_absence_with_confirmed_paid_leave_blocks_approval_until_reviewed(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")

    blocked = approve_run(client, run_id)
    assert blocked.status_code == 303
    assert blocked.headers["location"].endswith("error=absence_leave_conflict")

    page = client.get(f"/payroll/runs/{run_id}")
    assert "احتمال تعارض يحتاج مراجعة" in page.text

    token = csrf(page)
    review = client.post(
        f"/payroll/items/{item_id}/absence-leave-review",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert review.status_code == 303
    assert "error" not in review.headers["location"]

    approved = approve_run(client, run_id)
    assert approved.status_code == 303
    assert approved.headers["location"].rstrip("/").endswith(str(run_id))
    with client.test_session_factory() as db:
        assert db.get(PayrollRun, run_id).status == "approved"


def test_absence_without_leave_approves_normally(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    approved = approve_run(client, run_id)
    assert "error" not in approved.headers["location"]


def test_unpaid_or_draft_leave_is_not_a_conflict(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "UNPAID", "confirmed")
    add_leave(client, "ANNUAL", "draft")
    approved = approve_run(client, run_id)
    assert "error" not in approved.headers["location"]


def test_editing_absence_days_resets_review(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")
    page = client.get(f"/payroll/runs/{run_id}")
    token = csrf(page)
    client.post(
        f"/payroll/items/{item_id}/absence-leave-review",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    set_absence(client, item_id, 2)
    blocked = approve_run(client, run_id)
    assert blocked.headers["location"].endswith("error=absence_leave_conflict")


def test_stale_snapshot_warning_after_post_draft_salary_change(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id

    page = client.get(f"/payroll/runs/{run_id}")
    assert "الكشف مبني على اللقطة القديمة" not in page.text

    with client.test_session_factory() as db:
        employee = db.get(Employee, client.test_employee_id)
        db.add(EmployeeChange(
            employee_id=employee.id,
            field_name="basic_salary",
            old_value="6000.00",
            new_value="6500.00",
            effective_date=date(2026, 10, 1),
            reason="Synthetic post-draft change",

        ))
        employee.basic_salary = Decimal("6500.00")
        db.commit()

    page = client.get(f"/payroll/runs/{run_id}")
    assert "الكشف مبني على اللقطة القديمة" in page.text
    assert "MY-TEST-GRD-0001" in page.text


def test_mid_period_compensation_change_flags_stale(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id

    with client.test_session_factory() as db:
        employee = db.get(Employee, client.test_employee_id)
        db.add(EmployeeChange(
            employee_id=employee.id,
            field_name="daily_hours",
            old_value="8",
            new_value="8",
            effective_date=date(2026, 10, 15),
            reason="Synthetic mid-period hours change",

        ))
        db.commit()

    page = client.get(f"/payroll/runs/{run_id}")
    assert "الكشف مبني على اللقطة القديمة" in page.text


def test_leave_added_after_review_invalidates_confirmation(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")
    page = client.get(f"/payroll/runs/{run_id}")
    token = csrf(page)
    client.post(
        f"/payroll/items/{item_id}/absence-leave-review",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    # A second confirmed paid leave appears after the review: confirmation stale.
    with client.test_session_factory() as db:
        leave_type = db.scalar(select(LeaveType).where(LeaveType.code == "ANNUAL"))
        db.add(LeaveRecord(
            employee_id=client.test_employee_id,
            leave_type_id=leave_type.id,
            start_date=date(2026, 10, 20), end_date=date(2026, 10, 21),
            calendar_days=2, charged_days=Decimal("2"),
            reason="Synthetic later leave", status="confirmed", created_by="guard-admin",
        ))
        db.commit()
    blocked = approve_run(client, run_id)
    assert blocked.headers["location"].endswith("error=absence_leave_conflict")


def test_leave_cancelled_after_review_invalidates_confirmation(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")
    page = client.get(f"/payroll/runs/{run_id}")
    token = csrf(page)
    client.post(
        f"/payroll/items/{item_id}/absence-leave-review",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    with client.test_session_factory() as db:
        leave = db.scalar(select(LeaveRecord))
        leave.status = "cancelled"
        db.commit()
    # Conflict is gone entirely: approval passes without any review requirement.
    approved = approve_run(client, run_id)
    assert "error" not in approved.headers["location"]


def test_leave_date_edit_same_id_invalidates_confirmation(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")
    page = client.get(f"/payroll/runs/{run_id}")
    token = csrf(page)
    client.post(
        f"/payroll/items/{item_id}/absence-leave-review",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    # Same leave row, still confirmed and still overlapping, dates edited.
    with client.test_session_factory() as db:
        leave = db.scalar(select(LeaveRecord))
        leave.start_date = date(2026, 10, 15)
        leave.end_date = date(2026, 10, 17)
        db.commit()
    blocked = approve_run(client, run_id)
    assert blocked.headers["location"].endswith("error=absence_leave_conflict")


def test_save_with_unchanged_absence_keeps_review(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")
    page = client.get(f"/payroll/runs/{run_id}")
    token = csrf(page)
    client.post(
        f"/payroll/items/{item_id}/absence-leave-review",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    # ordinary save with the same absence value must not reset the confirmation
    set_absence(client, item_id, 1)
    approved = approve_run(client, run_id)
    assert approved.status_code == 303
    assert approved.headers["location"].rstrip("/").endswith(str(run_id))
    with client.test_session_factory() as db:
        assert db.get(PayrollRun, run_id).status == "approved"


def test_review_form_carries_no_financial_inputs(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")
    page = client.get(f"/payroll/runs/{run_id}")
    match = re.search(
        r'<form[^>]*action="/payroll/items/\d+/absence-leave-review"[^>]*>(.*?)</form>',
        page.text,
        re.DOTALL,
    )
    assert match, "standalone review form missing from run page"
    inputs = re.findall(r'<input[^>]*name="([^"]+)"', match.group(1))
    assert inputs == ["csrf_token"]
    buttons = re.findall(r'<button[^>]*formaction=', match.group(1))
    assert not buttons


def save_item(client, item_id, **overrides):
    page = client.get(f"/payroll/runs/{client.test_run_id}")
    token = csrf(page)
    data = {
        "csrf_token": token,
        "attendance_bonus": "0", "performance_bonus": "0", "occupancy_bonus": "0",
        "overtime_hours": "0", "other_bonus": "0", "absence_days": "0",
        "lateness_deduction": "0", "insurance_share": "0", "taxes": "0",
        "other_deductions": "0", "note": "",
    }
    data.update({k: str(v) for k, v in overrides.items()})
    response = client.post(
        f"/payroll/items/{item_id}/update", data=data, follow_redirects=False
    )
    assert response.status_code == 303
    assert "error" not in response.headers["location"]


def test_missing_employee_alert_on_run_page(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    page = client.get(f"/payroll/runs/{run_id}")
    assert "غير مشمولين في هذا المسير" not in page.text

    with client.test_session_factory() as db:
        late_hire = Employee(
            code="MY-TEST-GRD-0002",
            full_name="Synthetic Late Hire",
            department_id=1,
            position="Test Position",
            hire_date=date(2026, 10, 5),
            status="active",
            basic_salary=Decimal("4000.00"),
            daily_hours=Decimal("8"),
        )
        db.add(late_hire)
        db.flush()
        db.add(EmployeeServicePeriod(
            employee_id=late_hire.id,
            start_date=date(2026, 10, 5),
            end_date=None,
            start_reason="Synthetic late hire",
        ))
        db.commit()

    page = client.get(f"/payroll/runs/{run_id}")
    assert "غير مشمولين في هذا المسير" in page.text
    assert "MY-TEST-GRD-0002" in page.text


def test_unchanged_overtime_save_keeps_approval(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    save_item(client, item_id, overtime_hours=2)
    page = client.get(f"/payroll/runs/{run_id}")
    token = csrf(page)
    client.post(
        f"/payroll/items/{item_id}/overtime-approve",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    with client.test_session_factory() as db:
        assert db.get(PayrollItem, item_id).overtime_approved is True
    save_item(client, item_id, overtime_hours=2, other_bonus=50)
    with client.test_session_factory() as db:
        assert db.get(PayrollItem, item_id).overtime_approved is True


def test_changed_overtime_resets_approval(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    save_item(client, item_id, overtime_hours=2)
    page = client.get(f"/payroll/runs/{run_id}")
    token = csrf(page)
    client.post(
        f"/payroll/items/{item_id}/overtime-approve",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    save_item(client, item_id, overtime_hours=3)
    with client.test_session_factory() as db:
        assert db.get(PayrollItem, item_id).overtime_approved is False


def test_run_totals_rendered_on_page(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    save_item(client, item_id, other_bonus=100)
    page = client.get(f"/payroll/runs/{run_id}")
    assert "عدد الموظفين" in page.text
    assert "إجمالي الصافي" in page.text
    assert "إجمالي المتبقي" in page.text
    # single synthetic employee: 6000.00 net + 100.00 bonus
    assert "6100.00" in page.text


def test_edit_rows_collapsed_and_conflict_row_stays_open(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    page = client.get(f"/payroll/runs/{run_id}")
    assert 'id="employee-search"' in page.text
    assert '<tr class="payroll-edit-row" hidden>' in page.text
    assert '<tr class="payroll-edit-row">' not in page.text

    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")
    page = client.get(f"/payroll/runs/{run_id}")
    # a row with an unresolved absence/leave conflict stays expanded so the
    # review gate is never hidden behind a collapsed row
    assert '<tr class="payroll-edit-row">' in page.text
