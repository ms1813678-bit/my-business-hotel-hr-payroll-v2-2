"""On-demand weekly summary: detector, read-only builder, trilingual format."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    AdvanceRequest, AttendanceDailyRecord, Department, Employee, LeaveRecord,
    LeaveType, PayrollRun,
)
import app.weekly_summary as weekly


@pytest.fixture()
def seeded(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    today = date.today()
    with factory.begin() as db:
        department = Department(name="الاستقبال", active=True)
        db.add(department)
        db.flush()
        employee = Employee(
            code="MY-0001", full_name="موظف تجريبي", department_id=department.id,
            position="موظف", hire_date=today - timedelta(days=2),
            basic_salary=Decimal("6000.00"), daily_hours=Decimal("8.00"), status="active",
        )
        db.add(employee)
        leave_type = LeaveType(code="ANN", name_ar="سنوية", name_en="Annual", active=True)
        db.add(leave_type)
        db.flush()
        db.add(AttendanceDailyRecord(
            employee_id=employee.id, work_date=today - timedelta(days=1),
            status="needs_review",
        ))
        db.add(AttendanceDailyRecord(
            employee_id=employee.id, work_date=today - timedelta(days=2),
            status="approved",
        ))
        # outside the window: must not be counted
        db.add(AttendanceDailyRecord(
            employee_id=employee.id, work_date=today - timedelta(days=30),
            status="needs_review",
        ))
        db.add(LeaveRecord(
            employee_id=employee.id, leave_type_id=leave_type.id,
            start_date=today - timedelta(days=1), end_date=today + timedelta(days=1),
            calendar_days=3, charged_days=Decimal("3"), reason="t", status="draft",
            created_by="tester",
        ))
        db.add(AdvanceRequest(
            employee_id=employee.id, request_date=today - timedelta(days=3),
            amount=Decimal("1000.00"), payroll_month=date(2026, 11, 1), salary_basis=Decimal("6000.00"), reason="t", advance_type="regular",
            status="pending", created_by="tester",
        ))
        db.add(PayrollRun(
            period_start=date(2026, 11, 1), period_end=date(2026, 11, 30),
            pay_date=date(2026, 12, 1), status="approved", created_by="tester",
        ))
    monkeypatch.setattr(weekly, "SessionLocal", factory)
    yield factory
    engine.dispose()


def test_detector_phrases():
    assert weekly.is_weekly_summary_request("اعطني ملخص الأسبوع")
    assert weekly.is_weekly_summary_request("ملخص أسبوعي للعمل")
    assert weekly.is_weekly_summary_request("Weekly summary please")
    assert weekly.is_weekly_summary_request("本周总结")
    assert not weekly.is_weekly_summary_request("كم عدد الموظفين النشطين؟")
    assert not weekly.is_weekly_summary_request("")


def test_builder_counts_only_the_window(seeded):
    report = weekly.build_weekly_summary()
    assert report["attendance_review"] == 1  # 30-day-old review record excluded
    assert report["attendance_approved"] == 1
    assert report["leaves"].get("draft") == 1
    assert report["advances"].get("pending") == 1
    assert report["hires"] == 1
    assert report["latest_payroll"] == {"period": "2026-11-01", "status": "approved"}


def test_formatter_is_trilingual(seeded):
    report = weekly.build_weekly_summary()
    ar = weekly.format_weekly_summary(report, "ar")
    en = weekly.format_weekly_summary(report, "en")
    zh = weekly.format_weekly_summary(report, "zh")
    assert "ملخص الأسبوع" in ar
    assert "Weekly summary" in en and "Attendance: 1" in en
    assert "周度总结" in zh and "考勤：1" in zh
    # the figures are identical across languages
    for text in (ar, en, zh):
        assert "2026-11-01" in text


def test_dispatch_through_structured_query(seeded):
    from app.ai_hr_queries import is_structured_hr_query, answer_structured_hr_query

    assert is_structured_hr_query("ملخص الأسبوع")
    answer = answer_structured_hr_query("ملخص الأسبوع", language="ar")
    assert "ملخص الأسبوع" in answer
    answer_en = answer_structured_hr_query("Weekly summary", language="en")
    assert "Weekly summary" in answer_en
