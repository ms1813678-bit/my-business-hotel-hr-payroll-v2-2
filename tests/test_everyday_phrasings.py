"""Stage-3 understanding coverage: the same everyday question in varied
phrasings (AR/EN/ZH) must route to the same grounded deterministic answer.
Mutation phrasings must never be swallowed by read-only detectors."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import AttendanceDailyRecord, Department, Employee
import app.ai_hr_queries as queries
import app.weekly_summary as weekly
from app.usage_guide import is_usage_guide_request


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
        for index in (1, 2, 3):
            db.add(Employee(
                code=f"MY-000{index}", full_name=f"موظف {index}",
                department_id=department.id, position="موظف",
                hire_date=today - timedelta(days=30), basic_salary=Decimal("6000.00"),
                daily_hours=Decimal("8.00"), status="active",
            ))
        db.flush()
        db.add(AttendanceDailyRecord(
            employee_id=1, work_date=today, status="needs_review"))
        db.commit()
    monkeypatch.setattr(queries, "SessionLocal", factory)
    yield factory
    engine.dispose()


COUNT_PHRASINGS = [
    "كام موظف شغال عندنا؟",
    "كم عدد الموظفين النشطين؟",
    "عندنا كام واحد شغال؟",
    "how many employees do we have?",
    "how many active staff are there?",
    "number of employees",
    "我们有多少员工？",
    "多少员工在职？",
    "现在有几位员工？",
]

ATTENDANCE_TODAY_PHRASINGS = [
    "مين غايب النهارده؟",
    "مين اتأخر النهارده؟",
    "مين ما حضرش اليوم؟",
    "who is absent today?",
    "who was late today?",
    "今天谁没来？",
    "今天谁迟到？",
]

LEAVE_PHRASINGS = [
    "فاضل له كام يوم إجازة MY-0001؟",
    "رصيد إجازة MY-0001",
    "MY-0001 leave days left?",
    "remaining leave for MY-0001",
    "MY-0001 还剩多少假？",
]

ADVANCE_PHRASINGS = [
    "MY-0001 عليه سلفة؟",
    "سلف وأقساط MY-0001",
    "does MY-0001 have an advance?",
    "MY-0001 有预支吗？",
]

PAYROLL_PHRASINGS = [
    "مرتبه كام MY-0001؟",
    "صافي مرتب MY-0001 الشهر ده",
    "what is MY-0001's net salary?",
    "MY-0001 工资多少？",
]

STAFF_ACTION_PHRASINGS = [
    "MY-0001 اتجازى الشهر ده؟",
    "جزاءات MY-0001",
    "was MY-0001 sanctioned?",
    "MY-0001 被处罚了吗？",
]

GUIDE_PHRASINGS = [
    ("عايز أضيف حضور لموظف نسي يبصم", "/attendance/review"),
    ("عايز أعمل جزاء لموظف", "/staff-actions"),
    ("عايز أقفل الشهر واصرف المرتبات", "/payroll"),
    ("محتاج أضيف موظف جديد", "/employees"),
    ("how can i add a new employee", "/employees"),
    ("我想申请请假", "/leave"),
]

WEEKLY_PHRASINGS = [
    "ايه اللي حصل الاسبوع ده؟",
    "أخبار الأسبوع ايه؟",
    "what happened this week?",
    "how was this week at work?",
    "本周发生了什么？",
    "这周怎么样？",
]

MUTATION_PHRASINGS = [
    "امسح سجل الحضور بتاع امبارح",
    "عدّل مرتب MY-0001 لـ 9000",
    "delete all attendance records",
    "删除员工",
]


def test_command_requests_are_checked_before_read_only_answers():
    """Mutations keep going through the command plan + approval path first."""
    from app.ai_agent import is_command_request
    assert is_command_request("نفذ: اعتمد مسير الرواتب رقم 3")


def test_count_phrasings_route_to_grounded_count(seeded):
    for phrase in COUNT_PHRASINGS:
        assert queries.is_employee_count_request(phrase), phrase
        answer = queries.answer_structured_hr_query(phrase, "en")
        assert "Active employees right now: 3" in answer, phrase


def test_attendance_today_phrasings_route_to_today_analysis(seeded):
    for phrase in ATTENDANCE_TODAY_PHRASINGS:
        assert queries.is_attendance_analysis_request(phrase), phrase
        for language in ("ar", "en", "zh"):
            answer = queries.answer_structured_hr_query(phrase, language)
            assert "لم أتعرف" not in answer
            assert "could not recognize" not in answer
            assert "无法识别" not in answer


def test_leave_advance_payroll_staff_phrasings(seeded):
    for phrase in LEAVE_PHRASINGS:
        assert queries.is_leave_balance_request(phrase), phrase
    for phrase in ADVANCE_PHRASINGS:
        assert queries.is_advance_query_request(phrase), phrase
    for phrase in PAYROLL_PHRASINGS:
        assert queries.is_payroll_query_request(phrase), phrase
    for phrase in STAFF_ACTION_PHRASINGS:
        assert queries.is_staff_action_query_request(phrase), phrase


def test_guide_phrasings_resolve_to_real_topics():
    for phrase, path in GUIDE_PHRASINGS:
        topic = is_usage_guide_request(phrase)
        assert topic is not None, phrase
        assert topic["path"] == path, phrase


def test_weekly_phrasings():
    for phrase in WEEKLY_PHRASINGS:
        assert weekly.is_weekly_summary_request(phrase), phrase


def test_mutation_phrasings_never_match_read_only_detectors():
    for phrase in MUTATION_PHRASINGS:
        assert not queries.is_structured_hr_query(phrase), phrase


def test_count_report_is_db_grounded(seeded):
    report = queries.build_employee_count_report()
    assert report == {"active": 3, "total": 3}
    with seeded.begin() as db:
        employee = db.scalars(select(Employee).where(Employee.code == "MY-0003")).one()
        employee.status = "terminated"
    report = queries.build_employee_count_report()
    assert report == {"active": 2, "total": 3}
    assert "2" in queries.format_employee_count_report(report, "en")
    assert "2" in queries.format_employee_count_report(report, "ar")
    assert "2" in queries.format_employee_count_report(report, "zh")
