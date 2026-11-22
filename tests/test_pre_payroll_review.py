"""Pre-payroll review: read-only, deterministic, suggestions not verdicts."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    AttendanceDailyRecord, Department, Employee, PayrollItem, PayrollRun,
)
import app.pre_payroll_review as review


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, factory


def _employee(db, code="MY-0010"):
    dept = Department(name="Reception", active=True)
    db.add(dept)
    db.flush()
    emp = Employee(
        code=code, full_name="موظف مراجعة الرواتب", department_id=dept.id,
        position="Reception", hire_date=date(2025, 1, 1), status="active",
        basic_salary=Decimal("6000"), daily_hours=Decimal("8"),
    )
    db.add(emp)
    db.flush()
    return emp


def _run(db, month, status="draft"):
    run = PayrollRun(
        period_start=month, period_end=date(2026, month.month, 28),
        pay_date=date(2026, month.month, 28), status=status, created_by="test",
    )
    db.add(run)
    db.flush()
    return run


def _item(db, run, emp, absence="0", lateness="0", sanction="0", other="0"):
    item = PayrollItem(
        payroll_run_id=run.id, employee_id=emp.id,
        employee_code_snapshot=emp.code, employee_name_snapshot=emp.full_name,
        department_name_snapshot="Reception", position_snapshot="Reception",
        salary_basis=Decimal("6000"), base_salary=Decimal("6000"), daily_hours=Decimal("8"),
        absence_deduction=Decimal(absence), lateness_deduction=Decimal(lateness),
        staff_sanction_amount=Decimal(sanction), other_deductions=Decimal(other),
        net_pay=Decimal("6000"),
    )
    db.add(item)
    db.flush()
    return item


def test_no_run_states_so():
    engine, factory = _session()
    try:
        with factory() as db:
            pass
        orig = review.SessionLocal
        review.SessionLocal = factory
        try:
            report = review.build_pre_payroll_review(date(2026, 11, 1))
        finally:
            review.SessionLocal = orig
        assert report["run"]["status"] == "not_created"
        for lang, marker in (("ar", "لا يوجد مسير"), ("en", "No payroll run"), ("zh", "尚未创建工资批次")):
            assert marker in review.format_pre_payroll_review(report, lang)
    finally:
        engine.dispose()


def _patched(factory, monkeypatch):
    monkeypatch.setattr(review, "SessionLocal", factory)


def test_unsettled_attendance_and_unusual_deduction(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            emp = _employee(db)
            prev_run = _run(db, date(2026, 10, 1), status="approved")
            _item(db, prev_run, emp, lateness="50")
            run = _run(db, date(2026, 11, 1))
            item = _item(db, run, emp, lateness="400")
            db.add(AttendanceDailyRecord(
                employee_id=emp.id, work_date=date(2026, 11, 3), status="needs_review"))
            db.commit()
            item_id = item.id
        _patched(factory, monkeypatch)
        report = review.build_pre_payroll_review(date(2026, 11, 1))
        assert report["run"]["status"] == "draft"
        assert len(report["unsettled_attendance"]) == 1
        assert report["unsettled_attendance"][0]["records"][0]["work_date"] == "2026-11-03"
        assert len(report["unusual_deductions"]) == 1
        flagged = report["unusual_deductions"][0]
        assert flagged["current_deductions"] == "400.00"
        assert flagged["baseline_average"] == "50.00"
        assert flagged["item_id"] == item_id
        assert not report["insufficient_baseline"]
        text = review.format_pre_payroll_review(report, "en")
        assert "review payroll item #" in text and "2026-11-03" in text
        assert "suggestions only" in text
    finally:
        engine.dispose()


def test_normal_deduction_not_flagged_and_advance_excluded(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            emp = _employee(db)
            prev_run = _run(db, date(2026, 10, 1), status="approved")
            _item(db, prev_run, emp, lateness="300")
            run = _run(db, date(2026, 11, 1))
            item = _item(db, run, emp, lateness="350")
            item.advance_deduction = Decimal("2000")  # scheduled installment: not a flag
            db.commit()
        _patched(factory, monkeypatch)
        report = review.build_pre_payroll_review(date(2026, 11, 1))
        assert report["unusual_deductions"] == []
        assert report["insufficient_baseline"] == []
    finally:
        engine.dispose()


def test_first_month_employee_is_insufficient_baseline_not_no_issues(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            emp = _employee(db)
            run = _run(db, date(2026, 11, 1))
            _item(db, run, emp, lateness="500")
            db.commit()
        _patched(factory, monkeypatch)
        report = review.build_pre_payroll_review(date(2026, 11, 1))
        assert report["unusual_deductions"] == []
        assert len(report["insufficient_baseline"]) == 1
        text = review.format_pre_payroll_review(report, "zh")
        assert "这不代表没有问题" in text
    finally:
        engine.dispose()


def test_zero_deduction_first_month_not_listed(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            emp = _employee(db)
            run = _run(db, date(2026, 11, 1))
            _item(db, run, emp)
            db.commit()
        _patched(factory, monkeypatch)
        report = review.build_pre_payroll_review(date(2026, 11, 1))
        assert report["insufficient_baseline"] == []
        assert report["unusual_deductions"] == []
    finally:
        engine.dispose()


def test_ai_dispatch_trilingual(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            emp = _employee(db)
            run = _run(db, date(2026, 11, 1))
            _item(db, run, emp, lateness="500")
            db.commit()
        import app.ai_hr_queries as queries
        monkeypatch.setattr(review, "SessionLocal", factory)
        # answer_structured_hr_query builds the report via its own SessionLocal;
        # pre_payroll_review.SessionLocal is the one it uses.
        assert queries.is_pre_payroll_review_request("راجع الرواتب قبل الاعتماد 2026-11")
        assert queries.is_pre_payroll_review_request("pre-payroll review 2026-11")
        assert queries.is_pre_payroll_review_request("发薪前检查 2026-11")
        for lang, marker in (
            ("ar", "مراجعة ما قبل اعتماد رواتب"),
            ("en", "Pre-payroll review"),
            ("zh", "工资批准前检查"),
        ):
            text = queries.answer_structured_hr_query(
                {"ar": "راجع الرواتب قبل الاعتماد 2026-11",
                 "en": "pre-payroll review 2026-11",
                 "zh": "发薪前检查 2026-11"}[lang], language=lang)
            assert marker in text
            assert "500" in text
    finally:
        engine.dispose()


def test_missing_attendance_distinguished_and_leave_excused(monkeypatch):
    from app.models import LeaveRecord, LeaveType, SecurityShiftAssignment, ShiftPattern
    engine, factory = _session()
    try:
        with factory() as db:
            emp = _employee(db)
            run = _run(db, date(2026, 11, 1))
            _item(db, run, emp)
            pattern = ShiftPattern(
                department_id=emp.department_id, code="SEC-A", name="Security A",
                start_time=datetime(2026, 1, 1, 0, 0).time(),
                end_time=datetime(2026, 1, 1, 8, 0).time(),
            )
            db.add(pattern)
            db.flush()
            # assigned 3 days: one with a record (settled), one on confirmed
            # leave (excused), one with nothing (missing)
            for d in (2, 3, 4):
                db.add(SecurityShiftAssignment(employee_id=emp.id, work_date=date(2026, 11, d), shift_pattern_id=pattern.id))
            db.add(AttendanceDailyRecord(employee_id=emp.id, work_date=date(2026, 11, 2), status="approved"))
            lt = LeaveType(code="ANN", name_en="Annual", name_ar="سنوية", name_zh="年假")
            db.add(lt)
            db.flush()
            db.add(LeaveRecord(employee_id=emp.id, leave_type_id=lt.id,
                               start_date=date(2026, 11, 3), end_date=date(2026, 11, 3),
                               calendar_days=1, charged_days=Decimal("1"), reason="x",
                               status="confirmed", created_by="t"))
            db.commit()
        _patched(factory, monkeypatch)
        report = review.build_pre_payroll_review(date(2026, 11, 1))
        assert len(report["missing_attendance"]) == 1
        assert [d["work_date"] for d in report["missing_attendance"][0]["days"]] == ["2026-11-04"]
        assert report["coverage_unproven"] == []
        text = review.format_pre_payroll_review(report, "en")
        assert "2026-11-04" in text and "2026-11-03" not in text
    finally:
        engine.dispose()


def test_employee_without_assignments_is_coverage_unproven(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            emp = _employee(db)
            run = _run(db, date(2026, 11, 1))
            _item(db, run, emp)
            db.commit()
        _patched(factory, monkeypatch)
        report = review.build_pre_payroll_review(date(2026, 11, 1))
        assert report["missing_attendance"] == []
        assert len(report["coverage_unproven"]) == 1
        text = review.format_pre_payroll_review(report, "ar")
        assert "تغطية الحضور غير مثبتة" in text and "ليست تأكيدًا بالانتظام" in text
    finally:
        engine.dispose()


def test_heuristics_visible_and_configurable(monkeypatch):
    engine, factory = _session()
    try:
        with factory() as db:
            emp = _employee(db)
            prev_run = _run(db, date(2026, 10, 1), status="approved")
            _item(db, prev_run, emp, lateness="50")
            run = _run(db, date(2026, 11, 1))
            _item(db, run, emp, lateness="120")
            db.commit()
        _patched(factory, monkeypatch)
        # default floor 100: 120 >= 2*50 but 120-50 < 100 -> not flagged
        report = review.build_pre_payroll_review(date(2026, 11, 1))
        assert report["unusual_deductions"] == []
        # same data flagged when management lowers the floor
        report2 = review.build_pre_payroll_review(date(2026, 11, 1), abs_floor=Decimal("50"))
        assert len(report2["unusual_deductions"]) == 1
        text = review.format_pre_payroll_review(report, "en")
        assert "not adopted HR policy" in text
        assert "does not mean the deductions are correct" in text
    finally:
        engine.dispose()
