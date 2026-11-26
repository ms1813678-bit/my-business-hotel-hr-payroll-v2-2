"""Acceptance tests for the salary-advance workflow and repayment month."""
import re
from datetime import date as real_date
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdvanceInstallment, AdvanceRequest, AdminAccount, AuditLog, Department, Employee, EmployeeCodeCounter
from app.security import hash_password


@pytest.fixture()
def advance_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        admin = AdminAccount(
            username="advance-admin",
            password_hash=hash_password("Advance-Test-Password-123!"),
            active=True,
        )
        department = Department(name="Advance Test Department", active=True)
        db.add_all([admin, department, EmployeeCodeCounter(id=1, last_number=0)])
        db.flush()
        employee = Employee(
            code="MY-TEST-ADV-0001",
            full_name="Synthetic Advance Employee",
            department_id=department.id,
            position="Test Position",
            hire_date=real_date(2024, 1, 1),
            status="active",
            basic_salary=Decimal("5000.00"),
            daily_hours=Decimal("8"),
        )
        db.add(employee)
        db.commit()
        employee_id = employee.id

    class FixedDate(real_date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 17)

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", factory)
    monkeypatch.setattr("app.main.date", FixedDate)
    monkeypatch.setattr("app.advance_workflow.date", FixedDate)
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
            "username": "advance-admin",
            "password": "Advance-Test-Password-123!",
            "csrf_token": csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def submit_advance(
    client, token, amount="1250.00", request_date="2026-11-17",
    advance_type="regular", submission_key=None,
):
    return client.post(
        "/advances",
        data={
            "csrf_token": token,
            "employee_id": client.test_employee_id,
            "request_date": request_date,
            "amount": amount,
            "advance_type": advance_type,
            "reason": "Synthetic advance test request",
            "submission_key": submission_key or uuid4().hex,
        },
        follow_redirects=False,
    )


def test_regular_advance_cap_approval_payment_and_request_month(advance_client):
    client = advance_client
    login(client)
    token = csrf(client.get("/advances"))

    created = submit_advance(client, token)
    assert created.status_code == 303
    assert created.headers["location"] == "/advances"

    over_limit = submit_advance(client, token, amount="0.01")
    assert over_limit.status_code == 303
    assert "error=request" in over_limit.headers["location"]

    with client.test_session_factory() as db:
        request = db.scalar(select(AdvanceRequest))
        assert request.status == "pending"
        assert request.request_date == real_date(2026, 11, 17)
        assert request.payroll_month == real_date(2026, 11, 1)
        assert request.salary_basis == Decimal("5000.00")
        request_id = request.id

    approved = client.post(
        f"/advances/{request_id}/approve",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    paid = client.post(
        f"/advances/{request_id}/paid",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert paid.status_code == 303
    repeated = client.post(
        f"/advances/{request_id}/paid",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert repeated.status_code == 303
    assert "error=paid" in repeated.headers["location"]

    with client.test_session_factory() as db:
        request = db.get(AdvanceRequest, request_id)
        assert request.status == "paid"
        assert request.paid_by == "advance-admin"
        actions = db.scalars(select(AuditLog.action)).all()
        assert "advance_requested" in actions
        assert "advance_approved" in actions
        assert "advance_paid" in actions


def test_rejected_advance_releases_monthly_reserved_limit(advance_client):
    client = advance_client
    login(client)
    token = csrf(client.get("/advances"))

    assert submit_advance(client, token).status_code == 303
    with client.test_session_factory() as db:
        request_id = db.scalar(select(AdvanceRequest.id))

    rejected = client.post(
        f"/advances/{request_id}/reject",
        data={"csrf_token": token, "reason": "Synthetic rejection reason"},
        follow_redirects=False,
    )
    assert rejected.status_code == 303

    second = submit_advance(client, token)
    assert second.status_code == 303
    with client.test_session_factory() as db:
        requests = db.scalars(select(AdvanceRequest).order_by(AdvanceRequest.id)).all()
        assert [item.status for item in requests] == ["rejected", "pending"]


@pytest.mark.parametrize("request_date", ["2026-11-14", "2026-10-15", "2026-12-15"])
def test_advance_date_must_be_in_current_month_and_day_15_to_22(advance_client, request_date):
    client = advance_client
    login(client)
    token = csrf(client.get("/advances"))
    response = submit_advance(client, token, request_date=request_date)
    assert response.status_code == 303
    assert "error=request" in response.headers["location"]
    with client.test_session_factory() as db:
        assert db.scalars(select(AdvanceRequest)).all() == []


def test_advance_page_and_mutations_require_admin_and_csrf(advance_client):
    client = advance_client
    assert client.get("/advances").status_code == 401
    assert client.post("/advances", data={}).status_code == 403
    login(client)
    assert client.get("/advances").status_code == 200
    assert client.post("/advances/1/approve", data={}).status_code == 403


def test_installment_advance_bypasses_25_percent_cap_and_creates_schedule(advance_client):
    client = advance_client
    login(client)
    token = csrf(client.get("/advances"))

    created = submit_advance(
        client, token, amount="6000.00", advance_type="installment"
    )
    assert created.status_code == 303

    with client.test_session_factory() as db:
        request = db.scalar(select(AdvanceRequest))
        assert request.advance_type == "installment"
        assert request.payroll_month == real_date(2026, 11, 1)
        request_id = request.id

    approved = client.post(
        f"/advances/{request_id}/approve",
        data={"csrf_token": token, "installment_count": "6"},
        follow_redirects=False,
    )
    assert approved.status_code == 303

    with client.test_session_factory() as db:
        request = db.get(AdvanceRequest, request_id)
        assert request.status == "approved"
        rows = db.execute(
            select(AdvanceInstallment).where(
                AdvanceInstallment.advance_request_id == request_id
            ).order_by(AdvanceInstallment.installment_number)
        ).scalars().all()
        assert [row.amount for row in rows] == [Decimal("1000.00")] * 6
        assert [row.payroll_month for row in rows] == [
            real_date(2026, 11, 1), real_date(2026, 12, 1),
            real_date(2027, 1, 1), real_date(2027, 2, 1),
            real_date(2027, 3, 1), real_date(2027, 4, 1),
        ]



def test_advance_page_renders_employee_without_any_advances(advance_client):
    client = advance_client
    login(client)
    page = client.get("/advances")
    assert page.status_code == 200
    assert "MY-TEST-ADV-0001" in page.text
    assert "1250.00" in page.text and ">ج</span>" in page.text
    assert "0.00" in page.text


def test_advance_statuses_are_translated_to_arabic(advance_client):
    client = advance_client
    login(client)
    token = csrf(client.get("/advances"))

    assert submit_advance(client, token, amount="1000.00").status_code == 303
    assert submit_advance(client, token, amount="1500.00", advance_type="installment").status_code == 303
    assert submit_advance(client, token, amount="2000.00", advance_type="installment").status_code == 303
    assert submit_advance(client, token, amount="2500.00", advance_type="installment").status_code == 303

    with client.test_session_factory() as db:
        request_ids = db.scalars(
            select(AdvanceRequest.id).order_by(AdvanceRequest.id)
        ).all()

    assert client.post(
        f"/advances/{request_ids[1]}/approve",
        data={"csrf_token": token, "installment_count": "6"},
        follow_redirects=False,
    ).status_code == 303
    assert client.post(
        f"/advances/{request_ids[2]}/approve",
        data={"csrf_token": token, "installment_count": "1"},
        follow_redirects=False,
    ).status_code == 303
    assert client.post(
        f"/advances/{request_ids[2]}/paid",
        data={"csrf_token": token},
        follow_redirects=False,
    ).status_code == 303
    assert client.post(
        f"/advances/{request_ids[3]}/reject",
        data={"csrf_token": token, "reason": "Synthetic rejection"},
        follow_redirects=False,
    ).status_code == 303

    # Default tab is pending-first; each status label renders in its tab,
    # and the full log shows every status.
    page = client.get("/advances")
    assert page.status_code == 200
    assert "معلقة" in page.text
    assert "adv-tab-active" in page.text
    for stored_status in (">pending<", ">approved<", ">paid<", ">rejected<"):
        assert stored_status not in page.text
    assert "مصروفة" not in page.text
    assert "مرفوضة" not in page.text
    approved_tab = client.get("/advances?tab=approved")
    assert approved_tab.status_code == 200
    assert "معتمدة" in approved_tab.text
    assert "معلقة" in approved_tab.text  # tab label itself
    assert ">pending<" not in approved_tab.text
    all_tab = client.get("/advances?tab=all")
    assert all_tab.status_code == 200
    for label in ("معلقة", "معتمدة", "مصروفة", "مرفوضة"):
        assert label in all_tab.text


def test_regular_advance_approval_forces_one_installment_even_when_forged(advance_client):
    client = advance_client
    login(client)
    token = csrf(client.get("/advances"))
    assert submit_advance(client, token, amount="1250.00").status_code == 303
    assert submit_advance(
        client, token, amount="6000.00", advance_type="installment"
    ).status_code == 303

    page_before_approval = client.get("/advances")
    assert 'type="hidden" name="installment_count" value="1"' in page_before_approval.text
    assert 'name="installment_count" min="1" max="36"' in page_before_approval.text

    with client.test_session_factory() as db:
        request_id = db.scalar(
            select(AdvanceRequest.id).where(AdvanceRequest.advance_type == "regular")
        )

    approved = client.post(
        f"/advances/{request_id}/approve",
        data={"csrf_token": token, "installment_count": "12"},
        follow_redirects=False,
    )
    assert approved.status_code == 303

    with client.test_session_factory() as db:
        request = db.get(AdvanceRequest, request_id)
        rows = db.scalars(
            select(AdvanceInstallment).where(
                AdvanceInstallment.advance_request_id == request_id
            )
        ).all()
        assert request.status == "approved"
        assert len(rows) == 1
        assert rows[0].installment_number == 1

def test_duplicate_advance_form_submission_key_creates_only_one_request(advance_client):
    client = advance_client
    login(client)
    token = csrf(client.get("/advances"))
    submission_key = "a1b2c3d4" * 4

    first = submit_advance(
        client, token, amount="1000.00", advance_type="installment",
        submission_key=submission_key,
    )
    duplicate = submit_advance(
        client, token, amount="1000.00", advance_type="installment",
        submission_key=submission_key,
    )
    assert first.status_code == 303
    assert duplicate.status_code == 303

    with client.test_session_factory() as db:
        requests = db.scalars(select(AdvanceRequest)).all()
        submitted_actions = db.scalars(
            select(AuditLog).where(AuditLog.action == "advance_requested")
        ).all()
        idempotency_records = db.scalars(
            select(AuditLog).where(AuditLog.action == "advance_submission_accepted")
        ).all()
        assert len(requests) == 1
        assert len(submitted_actions) == 1
        assert len(idempotency_records) == 1
        assert idempotency_records[0].new_value == str(requests[0].id)

def test_duplicate_approval_rejection_and_payment_posts_do_not_duplicate_actions(advance_client):
    client = advance_client
    login(client)
    token = csrf(client.get("/advances"))

    # Use installment advances so these requests do not consume the regular cap.
    for amount in ("1000.00", "1100.00"):
        assert submit_advance(
            client, token, amount=amount, advance_type="installment"
        ).status_code == 303

    with client.test_session_factory() as db:
        request_ids = db.scalars(
            select(AdvanceRequest.id).order_by(AdvanceRequest.id)
        ).all()

    approve_url = f"/advances/{request_ids[0]}/approve"
    approve_data = {"csrf_token": token, "installment_count": "3"}
    assert client.post(approve_url, data=approve_data, follow_redirects=False).status_code == 303
    duplicate_approval = client.post(
        approve_url, data=approve_data, follow_redirects=False
    )
    assert duplicate_approval.status_code == 303
    assert "error=approve" in duplicate_approval.headers["location"]

    paid_url = f"/advances/{request_ids[0]}/paid"
    assert client.post(
        paid_url, data={"csrf_token": token}, follow_redirects=False
    ).status_code == 303
    duplicate_payment = client.post(
        paid_url, data={"csrf_token": token}, follow_redirects=False
    )
    assert duplicate_payment.status_code == 303
    assert "error=paid" in duplicate_payment.headers["location"]

    reject_url = f"/advances/{request_ids[1]}/reject"
    reject_data = {"csrf_token": token, "reason": "Synthetic rejection"}
    assert client.post(
        reject_url, data=reject_data, follow_redirects=False
    ).status_code == 303
    duplicate_rejection = client.post(
        reject_url, data=reject_data, follow_redirects=False
    )
    assert duplicate_rejection.status_code == 303
    assert "error=reject" in duplicate_rejection.headers["location"]

    with client.test_session_factory() as db:
        requests = db.scalars(
            select(AdvanceRequest).order_by(AdvanceRequest.id)
        ).all()
        assert [item.status for item in requests] == ["paid", "rejected"]
        for action in ("advance_approved", "advance_paid", "advance_rejected"):
            assert len(db.scalars(
                select(AuditLog).where(AuditLog.action == action)
            ).all()) == 1
        assert len(db.scalars(
            select(AdvanceInstallment).where(
                AdvanceInstallment.advance_request_id == request_ids[0]
            )
        ).all()) == 3



def test_advances_page_limit_uses_same_cap_basis_as_enforcement(advance_client, monkeypatch):
    """Page limit = 25% of (basic + this month's performance bonus when that month's item exists)."""
    from datetime import date as _d
    from decimal import Decimal as _D
    from app import main as m
    from app.models import PayrollItem, PayrollRun, Employee
    client = advance_client
    login(client)
    client.get("/advances")
    with m.SessionLocal() as db:
        emp = db.scalars(select(Employee)).first()
        start = _d.today().replace(day=1)
        run = PayrollRun(period_start=start, period_end=start, pay_date=start, status="draft", created_by="t")
        db.add(run); db.flush()
        db.add(PayrollItem(payroll_run_id=run.id, employee_id=emp.id, employee_code_snapshot="X",
                           employee_name_snapshot="A", department_name_snapshot="d", position_snapshot="p",
                           salary_basis=emp.basic_salary, base_salary=emp.basic_salary, daily_hours=_D("8"),
                           performance_bonus=_D("1000"), net_pay=_D("0"), overtime_amount=_D("0")))
        db.commit()
        expected = ((_D(str(emp.basic_salary)) + _D("1000")) * _D("0.25")).quantize(_D("0.01"))
        before = (_D(str(emp.basic_salary)) * _D("0.25")).quantize(_D("0.01"))
    after = client.get("/advances").text
    assert f"{expected:.2f}" in after and f"{before:.2f}" not in after.replace(f"{expected:.2f}", "")
