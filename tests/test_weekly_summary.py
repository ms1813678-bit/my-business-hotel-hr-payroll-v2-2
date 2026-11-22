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
    # the payroll status is localized, never the raw enum
    assert "الحالة: معتمد" in ar
    assert "status: approved" in en
    assert "状态：已批准" in zh
    # no activity qualifier while the window has records
    assert "تغطي السجلات المسجلة في النظام فقط" not in ar
    # every section links its records page (each point opens its record)
    assert "فتح السجلات: /attendance/review" in ar
    assert "فتح السجلات: /payroll" in ar
    assert "Open the records: /attendance/review" in en
    assert "Open the records: /leave" in en
    assert "Open the records: /advances" in en
    assert "打开记录: /attendance/review" in zh


def test_staff_actions_bucketed_by_cairo_calendar_day(seeded):
    """created_at is UTC; a Cairo day must decide the bucket, not the DB day."""
    from datetime import datetime, time, timezone
    from sqlalchemy import select as _select
    from app.models import StaffAction
    today = date.today()
    with seeded.begin() as db:
        emp = db.scalars(_select(Employee)).first()
        # 22:30 UTC on (today-7) = 00:30 Cairo on (today-6): inside the window
        db.add(StaffAction(
            employee_id=emp.id, kind="reward", category="general", title="t",
            reason="t", status="approved", created_by="t", is_financial=False,
            created_at=datetime.combine(today - timedelta(days=7), time(22, 30),
                                        tzinfo=timezone.utc),
        ))
        # 20:30 UTC on (today-7) = 22:30 Cairo on (today-7): outside the window
        db.add(StaffAction(
            employee_id=emp.id, kind="reward", category="general", title="t",
            reason="t", status="approved", created_by="t", is_financial=False,
            created_at=datetime.combine(today - timedelta(days=7), time(20, 30),
                                        tzinfo=timezone.utc),
        ))
    report = weekly.build_weekly_summary()
    assert report["staff_actions"] == 1


def test_empty_window_carries_source_coverage_qualifier():
    report = {
        "start": "2026-11-16", "end": "2026-11-22",
        "attendance_review": 0, "attendance_approved": 0,
        "leaves": {}, "advances": {}, "staff_actions": 0, "hires": 0,
        "latest_payroll": None,
    }
    ar = weekly.format_weekly_summary(report, "ar")
    en = weekly.format_weekly_summary(report, "en")
    zh = weekly.format_weekly_summary(report, "zh")
    assert "تغطي السجلات المسجلة في النظام فقط" in ar
    assert "cover only sources recorded in the system" in en
    assert "仅涵盖系统中已记录的来源" in zh


def test_dispatch_through_structured_query(seeded):
    from app.ai_hr_queries import is_structured_hr_query, answer_structured_hr_query

    assert is_structured_hr_query("ملخص الأسبوع")
    answer = answer_structured_hr_query("ملخص الأسبوع", language="ar")
    assert "ملخص الأسبوع" in answer
    answer_en = answer_structured_hr_query("Weekly summary", language="en")
    assert "Weekly summary" in answer_en
