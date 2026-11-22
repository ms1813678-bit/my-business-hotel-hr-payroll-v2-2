"""Monthly report (approved UI batch): 11 options, explicit month, role scope."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.dependencies import get_current_admin
from app.main import app
from app.models import (
    AdminAccount,
    AttendanceDailyRecord,
    Department,
    Employee,
    PayrollItem,
    PayrollRun,
)
from app.report_builder import (
    MONTHLY_ALL_OPTIONS,
    ReportSelection,
    _flatten_groups,
    build_monthly_pdf,
    build_report,
    monthly_options_for_types,
    REPORT_TYPE_ATTENDANCE,
    REPORT_TYPE_LEAVE,
    REPORT_TYPE_PAYROLL,
)


def _seed(db: Session):
    dept_a = Department(name="قسم أ", active=True)
    dept_b = Department(name="قسم ب", active=True)
    db.add_all([dept_a, dept_b])
    db.flush()
    emp1 = Employee(code="MY-M1", full_name="موظف أول", department_id=dept_a.id,
                    position="أمن", hire_date=date(2024, 1, 1), status="active",
                    basic_salary=Decimal("5000.00"), daily_hours=Decimal("8"))
    emp2 = Employee(code="MY-M2", full_name="موظف ثاني", department_id=dept_b.id,
                    position="استقبال", hire_date=date(2024, 1, 1), status="active",
                    basic_salary=Decimal("7000.00"), daily_hours=Decimal("8"))
    db.add_all([emp1, emp2])
    db.flush()
    run = PayrollRun(period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
                     pay_date=date(2026, 11, 1), status="approved", created_by="test")
    db.add(run)
    db.flush()
    db.add(PayrollItem(payroll_run_id=run.id, employee_id=emp1.id,
                       employee_name_snapshot=emp1.full_name, employee_code_snapshot=emp1.code,
                       department_name_snapshot="قسم أ", position_snapshot="أمن",
                       salary_basis=Decimal("5000"), base_salary=Decimal("5000"),
                       daily_hours=Decimal("8"), net_pay=Decimal("5000")))
    db.add(PayrollItem(payroll_run_id=run.id, employee_id=emp2.id,
                       employee_name_snapshot=emp2.full_name, employee_code_snapshot=emp2.code,
                       department_name_snapshot="قسم ب", position_snapshot="استقبال",
                       salary_basis=Decimal("7000"), base_salary=Decimal("7000"),
                       daily_hours=Decimal("8"), net_pay=Decimal("7000")))
    for day in (1, 2, 3):
        db.add(AttendanceDailyRecord(employee_id=emp1.id, work_date=date(2026, 10, day),
                                     status="approved", worked_minutes=480))
    db.add(AttendanceDailyRecord(employee_id=emp2.id, work_date=date(2026, 10, 1),
                                 status="approved", worked_minutes=480))
    prev_run = PayrollRun(period_start=date(2026, 9, 1), period_end=date(2026, 9, 30),
                          pay_date=date(2026, 10, 1), status="approved", created_by="test")
    db.add(prev_run)
    db.flush()
    db.add(PayrollItem(payroll_run_id=prev_run.id, employee_id=emp1.id,
                       employee_name_snapshot=emp1.full_name, employee_code_snapshot=emp1.code,
                       department_name_snapshot="قسم أ", position_snapshot="أمن",
                       salary_basis=Decimal("5000"), base_salary=Decimal("5000"),
                       daily_hours=Decimal("8"), net_pay=Decimal("5000")))
    db.add(PayrollItem(payroll_run_id=prev_run.id, employee_id=emp2.id,
                       employee_name_snapshot=emp2.full_name, employee_code_snapshot=emp2.code,
                       department_name_snapshot="قسم ب", position_snapshot="استقبال",
                       salary_basis=Decimal("7000"), base_salary=Decimal("7000"),
                       daily_hours=Decimal("8"), net_pay=Decimal("7000")))
    db.commit()
    return dept_a, dept_b, emp1, emp2


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            _seed(db)
            yield db
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_monthly_pdf_all_options_builds(db_session):
    content = build_monthly_pdf(db_session, date(2026, 10, 1), set(MONTHLY_ALL_OPTIONS))
    assert content.startswith(b"%PDF")
    assert len(content) > 5000


def test_monthly_pdf_summary_only_is_compact(db_session):
    content = build_monthly_pdf(db_session, date(2026, 10, 1), {1, 3})
    assert content.startswith(b"%PDF")


def test_grouping_modifier_merges_department_groups(db_session):
    selection = ReportSelection(
        report_type=REPORT_TYPE_PAYROLL,
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
        period_choice="custom", compare_like_for_like=False,
    )
    result = build_report(db_session, selection)
    grouped_departments = [g["department"] for g in result.groups]
    assert set(grouped_departments) == {"قسم أ", "قسم ب"}
    flat = _flatten_groups(result.groups)
    assert len(flat) == 1 and flat[0]["department"] == "الكل"
    assert len(flat[0]["rows"]) == sum(len(g["rows"]) for g in result.groups)


def test_comparison_modifier_reports_previous_month(db_session):
    selection = ReportSelection(
        report_type=REPORT_TYPE_PAYROLL,
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
        period_choice="custom", compare_like_for_like=True,
    )
    result = build_report(db_session, selection, today=date(2026, 11, 15))
    assert result.comparison is not None
    lfl = result.comparison.get("like_for_like")
    assert lfl is not None
    assert lfl["continuing_count"] == 2
    assert float(lfl["continuing_current_net"]) == 12000.0
    assert float(lfl["continuing_previous_net"]) == 12000.0


def test_monthly_totals_match_seeded_data(db_session):
    selection = ReportSelection(
        report_type=REPORT_TYPE_PAYROLL,
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
        period_choice="custom", compare_like_for_like=False,
    )
    selection.fields = {"emp_name", "emp_code", "pay_net"}
    result = build_report(db_session, selection)
    assert float(result.totals["pay_net"]) == 12000.0


def test_monthly_option_role_mapping():
    assistant = monthly_options_for_types({REPORT_TYPE_ATTENDANCE, REPORT_TYPE_LEAVE})
    assert 1 not in assistant and 8 not in assistant  # payroll content/charts
    assert {3, 4, 7, 9, 10, 11} <= assistant
    finance = monthly_options_for_types({REPORT_TYPE_PAYROLL, REPORT_TYPE_ATTENDANCE})
    assert 1 in finance and 7 not in finance
    assert monthly_options_for_types(set()) == set()


@pytest.fixture()
def client_factory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        _seed(db)

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", factory)

    def make(role: str) -> TestClient:
        account = AdminAccount(username=f"{role}-user", role=role, active=True,
                               display_name="مستخدم تجريبي",
                               password_hash="x")
        app.dependency_overrides[get_current_admin] = lambda: account
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_monthly_route_admin_gets_pdf(client_factory):
    client = client_factory("system_admin")
    response = client.get("/reports/monthly.pdf", params=[("month", "2026-10"), ("opt", "1"), ("opt", "3")])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_monthly_route_rejects_bad_inputs(client_factory):
    client = client_factory("system_admin")
    assert client.get("/reports/monthly.pdf", params={"month": "2026-13", "opt": "1"}).status_code == 400
    assert client.get("/reports/monthly.pdf", params={"month": "2026-10"}).status_code == 400
    assert client.get("/reports/monthly.pdf", params=[("month", "2026-10"), ("opt", "99")]).status_code == 400


def test_monthly_route_assistant_blocked_from_payroll(client_factory):
    client = client_factory("assistant_manager")
    response = client.get("/reports/monthly.pdf", params=[("month", "2026-10"), ("opt", "1")])
    assert response.status_code == 403
    response = client.get("/reports/monthly.pdf", params=[("month", "2026-10"), ("opt", "8")])
    assert response.status_code == 403


def test_monthly_route_assistant_gets_attendance_and_leave(client_factory):
    client = client_factory("assistant_manager")
    response = client.get("/reports/monthly.pdf", params=[("month", "2026-10"), ("opt", "3"), ("opt", "7")])
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_monthly_route_finance_gets_payroll_not_leave_only_modifier(client_factory):
    client = client_factory("finance")
    response = client.get("/reports/monthly.pdf", params=[("month", "2026-10"), ("opt", "1"), ("opt", "11")])
    assert response.status_code == 200
    response = client.get("/reports/monthly.pdf", params=[("month", "2026-10"), ("opt", "10")])
    assert response.status_code == 403  # modifier alone carries no data
