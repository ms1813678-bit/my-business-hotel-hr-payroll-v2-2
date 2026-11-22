from datetime import date, datetime
from types import SimpleNamespace
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    AttendanceDailyRecord,
    Department,
    Employee,
    LeaveBalanceTransaction,
    LeaveType,
    AdvanceRequest,
    AdvanceInstallment,
    StaffAction,
)
import app.ai_hr_queries as queries


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, factory


def test_request_detectors():
    assert queries.is_attendance_analysis_request("حلّل حضور يوم 2026-11-02")
    assert queries.is_leave_balance_request("رصيد إجازة الموظف MY-0001")
    assert queries.is_advance_query_request("سلف وأقساط المتبقية لـ MY-0001")
    assert queries.is_staff_action_query_request("جزاءات شهر 2026-11")
    assert not queries.is_attendance_analysis_request("قارن جدول الأمن مع البصمة")


def test_attendance_analysis_flags_late_and_missing(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            dept = Department(name="Reception", active=True)
            db.add(dept)
            db.flush()
            emp = Employee(
                code="MY-0001", full_name="موظف تجريبي", department_id=dept.id,
                position="Reception", hire_date=date(2026, 1, 1), status="active",
                basic_salary=Decimal("5000"), daily_hours=Decimal("8"),
            )
            db.add(emp)
            db.flush()
            db.add(AttendanceDailyRecord(
                employee_id=emp.id,
                work_date=date(2026, 11, 2),
                scheduled_start_at=datetime(2026, 11, 2, 9, 0),
                scheduled_end_at=datetime(2026, 11, 2, 17, 0),
                first_clock_in=datetime(2026, 11, 2, 9, 20),
                last_clock_out=None,
                status="needs_review",
            ))
            db.commit()

        monkeypatch.setattr(queries, "SessionLocal", factory)
        report = queries.build_attendance_analysis(work_date=date(2026, 11, 2), month_start=None)
        assert report["flagged_rows"] == 1
        text = queries.format_attendance_analysis_ar(report)
        assert "تأخير" in text or "انصراف" in text or "ملاحظات" in text
    finally:
        engine.dispose()


def test_leave_balance_report(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            dept = Department(name="Reception", active=True)
            db.add(dept)
            db.flush()
            emp = Employee(
                code="MY-0002", full_name="موظف إجازة", department_id=dept.id,
                position="Reception", hire_date=date(2026, 1, 1), status="active",
                basic_salary=Decimal("5000"), daily_hours=Decimal("8"),
            )
            leave_type = LeaveType(
                code="annual", name_ar="سنوية", name_en="Annual",
                is_paid=True, tracks_balance=True, active=True,
            )
            db.add_all([emp, leave_type])
            db.flush()
            db.add(LeaveBalanceTransaction(
                employee_id=emp.id, leave_type_id=leave_type.id,
                transaction_type="opening", days_delta=Decimal("15"),
                reason="رصيد افتتاحي", created_by="test",
            ))
            db.commit()

        monkeypatch.setattr(queries, "SessionLocal", factory)
        report = queries.build_leave_balance_report("رصيد إجازة MY-0002")
        assert report["employees"]
        assert report["employees"][0]["balances"][0]["days"] == "15.00"
        text = queries.format_leave_balance_ar(report)
        assert "سنوية" in text
    finally:
        engine.dispose()


def test_advance_and_staff_action_reports(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            dept = Department(name="Reception", active=True)
            db.add(dept)
            db.flush()
            emp = Employee(
                code="MY-0003", full_name="موظف سلفة", department_id=dept.id,
                position="Reception", hire_date=date(2026, 1, 1), status="active",
                basic_salary=Decimal("6000"), daily_hours=Decimal("8"),
            )
            db.add(emp)
            db.flush()
            req = AdvanceRequest(
                employee_id=emp.id, request_date=date(2026, 11, 16),
                payroll_month=date(2026, 11, 1), amount=Decimal("1200"),
                advance_type="installment", salary_basis=Decimal("6000"),
                reason="سلفة تجريبية", status="approved", created_by="test",
            )
            db.add(req)
            db.flush()
            now = datetime.utcnow()
            db.add(AdvanceInstallment(
                advance_request_id=req.id, installment_number=1,
                amount=Decimal("600"), payroll_month=date(2026, 12, 1),
                scheduled_payment_date=date(2026, 12, 5), status="scheduled",
                created_at=now,
            ))
            db.add(AdvanceInstallment(
                advance_request_id=req.id, installment_number=2,
                amount=Decimal("600"), payroll_month=date(2027, 1, 1),
                scheduled_payment_date=date(2027, 1, 5), status="scheduled",
                created_at=now,
            ))
            db.add(StaffAction(
                employee_id=emp.id, kind="sanction", category="lateness",
                title="تأخير", is_financial=True, amount=Decimal("50"),
                payroll_month=date(2026, 11, 1), reason="تأخير", status="approved",
                created_by="test",
            ))
            db.commit()

        monkeypatch.setattr(queries, "SessionLocal", factory)
        adv = queries.build_advance_report("سلف MY-0003")
        assert adv["items"]
        assert adv["items"][0]["remaining_installments"] == 2
        # A specific but unknown employee must not fall back to everyone else's data.
        assert queries.build_advance_report("سلف MY-9999")["items"] == []
        assert queries.build_advance_report("سلف الموظفين")["items"]
        staff = queries.build_staff_actions_report("جزاءات 2026-11")
        assert staff["items"]
        assert staff["items"][0]["kind"] == "sanction"
        assert queries.build_staff_actions_report("جزاءات MY-9999 2026-11")["items"] == []
    finally:
        engine.dispose()


def test_router_answers_without_model(monkeypatch):
    engine, factory = _session()
    try:
        monkeypatch.setattr(queries, "SessionLocal", factory)
        text = queries.answer_structured_hr_query("رصيد إجازة بدون موظف")
        assert "حدد الموظف" in text or "أرصدة" in text or "لا توجد"
    finally:
        engine.dispose()


def test_employee_profile_query_reads_registered_employee(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            dept = Department(name="Reception", active=True)
            db.add(dept)
            db.flush()
            emp = Employee(
                code="MY-0042", full_name="موظف ملف تجريبي", department_id=dept.id,
                position="Reception", hire_date=date(2026, 1, 1), status="active",
                basic_salary=Decimal("5200"), daily_hours=Decimal("8"),
            )
            db.add(emp)
            db.commit()
        monkeypatch.setattr(queries, "SessionLocal", factory)
        report = queries.build_employee_report("بيانات الموظف MY-0042")
        assert len(report["items"]) == 1
        assert report["items"][0]["code"] == "MY-0042"
        assert report["items"][0]["basic_salary"] == "5200.00"
        assert "MY-0042" in queries.format_employee_report_ar(report)
    finally:
        engine.dispose()


def test_payroll_query_detector_and_missing_month(monkeypatch):
    assert queries.is_payroll_query_request("راتب الموظف MY-0001 لشهر 2026-11")
    engine, factory = _session()
    try:
        monkeypatch.setattr(queries, "SessionLocal", factory)
        report = queries.build_payroll_report("مسير الرواتب 2026-11")
        assert report["status"] == "not_created"
    finally:
        engine.dispose()


def test_employee_filter_detector_distinguishes_singular_from_plural():
    assert queries._employee_filter_requested("راتب الموظف MY-9999")
    assert queries._employee_filter_requested("سلف الموظف أحمد")
    assert not queries._employee_filter_requested("سلف الموظفين")
    assert not queries._employee_filter_requested("جزاءات الموظفين لشهر 2026-11")


def _record(**kwargs):
    defaults = {column.name: None for column in AttendanceDailyRecord.__table__.columns}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_dst_spring_forward_shift_not_false_flagged():
    # Cairo 2026 spring forward: 2026-04-24 00:00-01:00 is skipped, so the
    # 23:00-07:00 wall-clock shift is 7 elapsed hours, not 8.
    record = _record(
        status="approved",
        work_date=date(2026, 4, 23),
        scheduled_start_at=datetime(2026, 4, 23, 23, 0),
        scheduled_end_at=datetime(2026, 4, 24, 7, 0),
        first_clock_in=datetime(2026, 4, 23, 23, 0),
        last_clock_out=datetime(2026, 4, 24, 7, 0),
        worked_minutes=420,
    )
    keys = [key for key, _ in queries._classify_attendance_row(record)]
    assert "unexpected_duration" not in keys
    assert "late_in" not in keys and "early_out" not in keys


def test_genuinely_short_shift_still_flagged_on_dst_night():
    record = _record(
        status="approved",
        work_date=date(2026, 4, 23),
        scheduled_start_at=datetime(2026, 4, 23, 23, 0),
        scheduled_end_at=datetime(2026, 4, 24, 7, 0),
        first_clock_in=datetime(2026, 4, 23, 23, 0),
        last_clock_out=datetime(2026, 4, 24, 5, 30),
        worked_minutes=330,
    )
    flags = dict(queries._classify_attendance_row(record))
    assert "unexpected_duration" in flags
    assert flags["early_out"] == 90


def test_dst_fall_back_shift_not_false_flagged():
    # Fall back 2026-10-29: 23:00-24:00 repeats, so 21:00-06:00 wall is
    # 10 elapsed hours when endpoints are unambiguous.
    record = _record(
        status="approved",
        work_date=date(2026, 10, 29),
        scheduled_start_at=datetime(2026, 10, 29, 21, 0),
        scheduled_end_at=datetime(2026, 10, 30, 6, 0),
        first_clock_in=datetime(2026, 10, 29, 21, 0),
        last_clock_out=datetime(2026, 10, 30, 6, 0),
        worked_minutes=600,
    )
    keys = [key for key, _ in queries._classify_attendance_row(record)]
    assert "unexpected_duration" not in keys
