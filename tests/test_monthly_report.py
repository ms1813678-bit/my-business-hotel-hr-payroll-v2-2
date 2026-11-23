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


def test_custom_full_month_previous_period_unchanged_base_semantics(db_session):
    # An August run must not leak into October's previous-month comparison
    # (the old 31/08 bound overlapped the August run and double-counted).
    employee = db_session.scalars(select(Employee).where(Employee.code == "MY-M1")).one()
    aug_run = PayrollRun(period_start=date(2026, 8, 1), period_end=date(2026, 8, 31),
                         pay_date=date(2026, 9, 1), status="approved", created_by="test")
    db_session.add(aug_run)
    db_session.flush()
    db_session.add(PayrollItem(payroll_run_id=aug_run.id, employee_id=employee.id,
                               employee_name_snapshot=employee.full_name, employee_code_snapshot=employee.code,
                               department_name_snapshot="قسم أ", position_snapshot="أمن",
                               salary_basis=Decimal("5000"), base_salary=Decimal("5000"),
                               daily_hours=Decimal("8"), net_pay=Decimal("999")))
    db_session.commit()
    selection = ReportSelection(
        report_type=REPORT_TYPE_PAYROLL,
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
        period_choice="custom", compare_like_for_like=True,
    )
    result = build_report(db_session, selection, today=date(2026, 11, 15))
    # Custom report semantics are preserved unchanged (owner constraint): the
    # previous period is the equal-length shift (31/08–30/09), which overlaps
    # both runs. The calendar-month bounds live in the monthly wrapper only
    # (_previous_calendar_month, tested via _payroll_month_summary).
    assert result.comparison["period_label"] == "31/08/2026 – 30/09/2026"
    net_row = next(row for row in result.comparison["items"] if row[0] == "الصافي")
    assert float(net_row[2]) == 12999.0  # equal-length shift overlaps August + September runs


def test_monthly_calendar_comparison_excludes_month_before_last(db_session):
    # Regression: the monthly-PDF sections must compare against the previous
    # calendar month only. With the equal-length shift, a 31-day October
    # caught the August run too (31/08 overlap) and doubled the prior net.
    employee = db_session.scalars(select(Employee).where(Employee.code == "MY-M1")).one()
    aug_run = PayrollRun(period_start=date(2026, 8, 1), period_end=date(2026, 8, 31),
                         pay_date=date(2026, 9, 1), status="approved", created_by="test")
    db_session.add(aug_run)
    db_session.flush()
    db_session.add(PayrollItem(payroll_run_id=aug_run.id, employee_id=employee.id,
                               employee_name_snapshot=employee.full_name, employee_code_snapshot=employee.code,
                               department_name_snapshot="قسم أ", position_snapshot="أمن",
                               salary_basis=Decimal("5000"), base_salary=Decimal("5000"),
                               daily_hours=Decimal("8"), net_pay=Decimal("999")))
    db_session.commit()
    selection = ReportSelection(
        report_type=REPORT_TYPE_PAYROLL,
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
        period_choice="custom", compare_like_for_like=True,
        monthly_calendar_comparison=True,
    )
    result = build_report(db_session, selection, today=date(2026, 11, 15))
    assert result.comparison["period_label"] == "01/09/2026 – 30/09/2026"
    net_row = next(row for row in result.comparison["items"] if row[0] == "الصافي")
    assert float(net_row[2]) == 12000.0  # September only; the August 999 is excluded
    lfl = result.comparison["like_for_like"]
    assert float(lfl["continuing_previous_net"]) == 12000.0


def test_monthly_wrapper_marks_section_selections(db_session):
    # The monthly PDF path must carry the calendar-comparison flag into every
    # section selection it composes; guard against someone dropping the line.
    import app.report_builder as rb

    seen = []
    original = rb.build_report

    def spy(db, selection, today=None):
        seen.append(selection.monthly_calendar_comparison)
        return original(db, selection, today=today)

    rb.build_report = spy
    try:
        content = build_monthly_pdf(db_session, date(2026, 10, 1), {2, 4, 10})
    finally:
        rb.build_report = original
    assert content.startswith(b"%PDF")
    assert seen and all(seen)


def test_payroll_month_summary_is_totals_only(db_session):
    from app.report_builder import _payroll_month_summary
    summary = _payroll_month_summary(db_session, date(2026, 10, 1), date(2026, 10, 31))
    current, previous = summary["current"], summary["previous"]
    assert float(current["base"]) == 12000.0
    assert float(current["net"]) == 12000.0
    assert float(current["allowances"]) == 0.0
    assert float(current["deductions"]) == 0.0
    assert float(current["advances"]) == 0.0
    assert current["employees"] == 2
    assert float(previous["net"]) == 12000.0
    assert summary["period_label"] == "01/09/2026 – 30/09/2026"
    # Draft runs never enter the summary, consistent with the monthly charts.
    oct_run = db_session.scalars(
        select(PayrollRun).where(PayrollRun.period_start == date(2026, 10, 1))
    ).one()
    oct_run.status = "draft"
    db_session.commit()
    draft_summary = _payroll_month_summary(db_session, date(2026, 10, 1), date(2026, 10, 31))
    assert draft_summary["current"]["employees"] == 0


def test_absence_series_counts_approved_run_absence_days(db_session):
    from app.report_builder import _monthly_six_month_series
    run = db_session.scalars(
        select(PayrollRun).where(PayrollRun.period_start == date(2026, 10, 1))
    ).one()
    item = db_session.scalars(
        select(PayrollItem).where(PayrollItem.payroll_run_id == run.id)
    ).first()
    item.absence_days = Decimal("2")
    db_session.commit()
    assert dict(_monthly_six_month_series(db_session, date(2026, 10, 1), "absence"))["10/2026"] == 2.0
    run.status = "draft"  # unapproved runs never enter the chart
    db_session.commit()
    # A month without an approved run is no-data (None, rendered as a gap),
    # not a misleading zero.
    assert dict(_monthly_six_month_series(db_session, date(2026, 10, 1), "absence"))["10/2026"] is None


def test_mixed_forbidden_and_allowed_options_refused(client_factory):
    client = client_factory("assistant_manager")
    response = client.get("/reports/monthly.pdf", params=[("month", "2026-10"), ("opt", "1"), ("opt", "3")])
    assert response.status_code == 403
    assert "ملخص الرواتب" in response.json()["detail"]  # names the rejected option, no silent partial report


def test_leave_aggregate_totals_only(db_session):
    from app.models import LeaveRecord, LeaveType
    from app.report_builder import _monthly_leave_aggregate
    leave_type = LeaveType(code="ANNUAL", name_ar="سنوية", name_en="Annual")
    db_session.add(leave_type)
    db_session.flush()
    emp = db_session.scalars(select(Employee).where(Employee.code == "MY-M1")).one()
    db_session.add(LeaveRecord(employee_id=emp.id, leave_type_id=leave_type.id,
                               start_date=date(2026, 10, 5), end_date=date(2026, 10, 7),
                               calendar_days=3, charged_days=Decimal("3"), status="confirmed",
                               reason="اختبار", created_by="test"))
    db_session.add(LeaveRecord(employee_id=emp.id, leave_type_id=leave_type.id,
                               start_date=date(2026, 10, 10), end_date=date(2026, 10, 10),
                               calendar_days=1, charged_days=Decimal("1"), status="draft",
                               reason="اختبار", created_by="test"))
    db_session.commit()
    rows = _monthly_leave_aggregate(db_session, date(2026, 10, 1), date(2026, 10, 31))
    assert rows == [{"leave_type": "سنوية", "count": 1, "days": Decimal(3), "charged": Decimal(3)}]
    assert "موظف" not in str(rows) and "MY-M1" not in str(rows)


def test_monthly_pdf_assistant_aggregate_leave_builds(db_session):
    content = build_monthly_pdf(db_session, date(2026, 10, 1), {7}, aggregate_leave=True)
    assert content.startswith(b"%PDF")


def test_summary_grouping_modifier_adds_department_breakdown(db_session):
    from app.report_builder import _payroll_month_summary
    summary = _payroll_month_summary(db_session, date(2026, 10, 1), date(2026, 10, 31))
    departments = {row["department"]: row for row in summary["by_department"]}
    assert float(departments["قسم أ"]["net"]) == 5000.0
    assert float(departments["قسم ب"]["net"]) == 7000.0
    assert departments["قسم أ"]["employees"] == 1
    content = build_monthly_pdf(db_session, date(2026, 10, 1), {1, 11})
    assert content.startswith(b"%PDF")


def test_previous_period_custom_range_unchanged(db_session):
    from app.report_builder import _previous_calendar_month, _previous_period
    # Non-calendar-month selections keep the original equal-length shift.
    assert _previous_period(date(2026, 10, 5), date(2026, 10, 19)) == (date(2026, 9, 20), date(2026, 10, 4))
    # The CUSTOM report keeps its original equal-length shift even for full
    # calendar months (owner constraint: custom flow unchanged).
    assert _previous_period(date(2026, 10, 1), date(2026, 10, 31)) == (date(2026, 8, 31), date(2026, 9, 30))
    # The MONTHLY wrapper alone uses exact previous-calendar-month bounds.
    assert _previous_calendar_month(date(2026, 10, 1), date(2026, 10, 31)) == (date(2026, 9, 1), date(2026, 9, 30))
    assert _previous_calendar_month(date(2026, 10, 5), date(2026, 10, 19)) == (date(2026, 9, 20), date(2026, 10, 4))


def test_approved_only_excludes_draft_payroll_and_unapproved_attendance(db_session):
    """Owner decision 2026-11-23: the monthly PDF's payroll/attendance sections
    show approved data only; the custom engine (approved_only=False, the
    default) is unchanged."""
    dept = Department(name="قسم الفلترة", active=True)
    db_session.add(dept)
    db_session.flush()
    emp = Employee(code="MY-AO", full_name="موظف الفلترة", department_id=dept.id,
                   position="أمن", hire_date=date(2024, 1, 1), status="active",
                   basic_salary=Decimal("5000.00"), daily_hours=Decimal("8"))
    db_session.add(emp)
    db_session.flush()
    run = PayrollRun(period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
                     pay_date=date(2026, 8, 1), status="draft", created_by="test")
    db_session.add(run)
    db_session.flush()
    db_session.add(PayrollItem(payroll_run_id=run.id, employee_id=emp.id,
                               employee_name_snapshot=emp.full_name, employee_code_snapshot=emp.code,
                               department_name_snapshot="قسم الفلترة", position_snapshot="أمن",
                               salary_basis=Decimal("5000"), base_salary=Decimal("5000"),
                               daily_hours=Decimal("8"), net_pay=Decimal("5000")))
    db_session.add(AttendanceDailyRecord(employee_id=emp.id, work_date=date(2026, 7, 3),
                                         status="approved", worked_minutes=480))
    db_session.add(AttendanceDailyRecord(employee_id=emp.id, work_date=date(2026, 7, 4),
                                         status="needs_review", worked_minutes=480))
    db_session.commit()

    base = dict(report_type=REPORT_TYPE_PAYROLL, start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 31), period_choice="custom",
                compare_like_for_like=False)
    # Default (custom report) semantics: drafts and unreviewed records included.
    custom = build_report(db_session, ReportSelection(**base))
    assert custom.totals["pay_net"] == Decimal("5000")
    # Monthly approved-only: the draft run's items vanish.
    monthly = build_report(db_session, ReportSelection(**base, approved_only=True))
    assert monthly.totals.get("pay_net", Decimal(0)) == Decimal(0)
    run.status = "approved"
    db_session.commit()
    monthly = build_report(db_session, ReportSelection(**base, approved_only=True))
    assert monthly.totals["pay_net"] == Decimal("5000")

    att_base = dict(base, report_type=REPORT_TYPE_ATTENDANCE)
    custom_att = build_report(db_session, ReportSelection(**att_base))
    row = next(r for g in custom_att.groups for r in g["rows"] if r["employee_id"] == emp.id)
    assert row["att_present"] == 1  # present was always approved-only
    # ...but the custom report still SURFACES the unreviewed day.
    assert dict(custom_att.summary)["أيام غير محسومة"] == 1
    monthly_att = build_report(db_session, ReportSelection(**att_base, approved_only=True))
    row = next(r for g in monthly_att.groups for r in g["rows"] if r["employee_id"] == emp.id)
    assert row["att_present"] == 1
    # Monthly approved-only: unapproved records no longer appear at all.
    assert dict(monthly_att.summary)["أيام غير محسومة"] == 0


def test_monthly_pdf_payroll_attendance_sections_carry_approved_note(db_session):
    """The monthly wrapper sets approved_only for payroll/attendance options and
    leaves advances/actions/leave sections on their status-labelled listings."""
    from app import report_builder

    seen = {}
    real_build_report = report_builder.build_report

    def spy(db, selection, today=None):
        seen[selection.report_type] = selection.approved_only
        return real_build_report(db, selection, today=today)

    report_builder.build_report = spy
    try:
        build_monthly_pdf(db_session, date(2026, 10, 1), {2, 3, 5, 6, 7})
    finally:
        report_builder.build_report = real_build_report
    assert seen[REPORT_TYPE_PAYROLL] is True
    assert seen[REPORT_TYPE_ATTENDANCE] is True
    assert seen[report_builder.REPORT_TYPE_ADVANCES] is False
    assert seen[report_builder.REPORT_TYPE_STAFF_ACTIONS] is False
    assert seen[report_builder.REPORT_TYPE_LEAVE] is False


def test_leave_aggregate_cross_month_clipped_charged_once(db_session):
    """Owner decision 2026-11-23 (report-only): a leave spanning months shows
    each month's own calendar days in that month; the admin-entered charged
    days count once, in the starting month. No balance/payroll change."""
    from app.models import LeaveRecord, LeaveType
    from app.report_builder import _monthly_leave_aggregate
    leave_type = LeaveType(code="ANNUAL", name_ar="سنوية", name_en="Annual")
    db_session.add(leave_type)
    db_session.flush()
    emp = db_session.scalars(select(Employee).where(Employee.code == "MY-M1")).one()
    # 28/09 – 03/10: calendar span 6, admin charged 5.
    db_session.add(LeaveRecord(employee_id=emp.id, leave_type_id=leave_type.id,
                               start_date=date(2026, 9, 28), end_date=date(2026, 10, 3),
                               calendar_days=6, charged_days=Decimal("5"), status="confirmed",
                               reason="اختبار", created_by="test"))
    db_session.commit()
    sep = _monthly_leave_aggregate(db_session, date(2026, 9, 1), date(2026, 9, 30))
    assert sep == [{"leave_type": "سنوية", "count": 1, "days": Decimal(3), "charged": Decimal(5)}]
    oct_rows = _monthly_leave_aggregate(db_session, date(2026, 10, 1), date(2026, 10, 31))
    assert oct_rows == [{"leave_type": "سنوية", "count": 1, "days": Decimal(3), "charged": Decimal(0)}]


def test_leave_detail_cross_month_clipped_consistent_with_aggregate(db_session):
    """Monthly leave DETAIL must match the aggregate policy (peer finding):
    a leave 28/09-03/10 shows 3 days + charged 5 in Sep and 3 days + charged 0
    in Oct per employee row and totals, not the full 6/5 in both months.
    The custom report (default flag) keeps the full record values."""
    from app.models import LeaveRecord, LeaveType
    from app.report_builder import REPORT_TYPE_LEAVE, _monthly_leave_aggregate

    leave_type = LeaveType(code="ANNUAL", name_ar="سنوية", name_en="Annual")
    db_session.add(leave_type)
    db_session.flush()
    emp = db_session.scalars(select(Employee).where(Employee.code == "MY-M1")).one()
    db_session.add(LeaveRecord(employee_id=emp.id, leave_type_id=leave_type.id,
                               start_date=date(2026, 9, 28), end_date=date(2026, 10, 3),
                               calendar_days=6, charged_days=Decimal("5"), status="confirmed",
                               reason="اختبار", created_by="test"))
    db_session.commit()

    def detail(month_start, month_end, clipping):
        result = build_report(db_session, ReportSelection(
            report_type=REPORT_TYPE_LEAVE,
            start_date=month_start, end_date=month_end,
            period_choice="custom", monthly_leave_clipping=clipping,
        ))
        rows = [r for g in result.groups for r in g["rows"] if r["employee_id"] == emp.id]
        assert len(rows) == 1
        return rows[0], result.totals

    sep_row, sep_totals = detail(date(2026, 9, 1), date(2026, 9, 30), True)
    assert sep_row["calendar_days"] == 3 and sep_row["charged_days"] == Decimal("5")
    assert sep_row["leave_start"] == "28/09/2026" and sep_row["leave_end"] == "03/10/2026"
    assert sep_totals["calendar_days"] == 3 and sep_totals["charged_days"] == 5.0

    oct_row, oct_totals = detail(date(2026, 10, 1), date(2026, 10, 31), True)
    assert oct_row["calendar_days"] == 3 and oct_row["charged_days"] == 0
    assert oct_row["leave_start"] == "28/09/2026" and oct_row["leave_end"] == "03/10/2026"
    assert oct_totals["calendar_days"] == 3 and oct_totals["charged_days"] == 0.0

    # Detail and aggregate now agree for both months.
    sep_agg = _monthly_leave_aggregate(db_session, date(2026, 9, 1), date(2026, 9, 30))
    oct_agg = _monthly_leave_aggregate(db_session, date(2026, 10, 1), date(2026, 10, 31))
    assert (sep_agg[0]["days"], sep_agg[0]["charged"]) == (Decimal(3), Decimal(5))
    assert (oct_agg[0]["days"], oct_agg[0]["charged"]) == (Decimal(3), Decimal(0))

    # Custom report unchanged: full record values, no clipping.
    custom_row, custom_totals = detail(date(2026, 9, 1), date(2026, 9, 30), False)
    assert custom_row["calendar_days"] == 6 and custom_row["charged_days"] == Decimal("5")
    assert custom_totals["calendar_days"] == 6
