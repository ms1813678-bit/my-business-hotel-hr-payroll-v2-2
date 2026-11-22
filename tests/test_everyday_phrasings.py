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
    assert report == {"scope": "all", "active": 3, "total": 3}
    with seeded.begin() as db:
        employee = db.scalars(select(Employee).where(Employee.code == "MY-0003")).one()
        employee.status = "terminated"
    report = queries.build_employee_count_report()
    assert report == {"scope": "all", "active": 2, "total": 3}
    assert "2" in queries.format_employee_count_report(report, "en")
    assert "2" in queries.format_employee_count_report(report, "ar")
    assert "2" in queries.format_employee_count_report(report, "zh")


SCOPED_COUNT_PHRASINGS = [
    "كام موظف اتأخر النهارده؟",
    "كام موظف غايب النهارده؟",
    "how many staff were late today?",
    "how many employees are on leave this month?",
    "今天有多少员工迟到？",
]


def test_scoped_count_questions_route_to_their_domain_not_headcount(seeded):
    for phrase in SCOPED_COUNT_PHRASINGS:
        assert not queries.is_employee_count_request(phrase), phrase
    # late/absent scoped counts land on the attendance analysis
    assert queries.is_attendance_analysis_request("كام موظف اتأخر النهارده؟")
    assert queries.is_attendance_analysis_request("how many staff were late today?")
    assert queries.is_attendance_analysis_request("今天有多少员工迟到？")


def test_day_analysis_states_its_coverage_limit(seeded):
    for language, needle in (("ar", "لا يمكن الجزم بغياب من لا سجل له"),
                             ("en", "cannot determine whether someone with no record was absent"),
                             ("zh", "无法判断没有记录的人是缺勤还是尚未录入")):
        answer = queries.answer_structured_hr_query("مين غايب النهارده؟", language)
        assert needle in answer, language


def test_month_analysis_has_no_day_coverage_line(seeded):
    answer = queries.answer_structured_hr_query("حلّل حضور شهر 2026-11", "ar")
    assert "لا يمكن الجزم بغياب" not in answer
def test_department_scoped_count_answers_the_department(seeded):
    answer = queries.answer_structured_hr_query("كام موظف في قسم الاستقبال؟", language="ar")
    assert "قسم الاستقبال" in answer
    assert ": 3" in answer
    assert "إجمالي المسجلين" not in answer  # not the whole-hotel headcount


def test_unknown_department_count_declines_honestly(seeded):
    answer = queries.answer_structured_hr_query("كام موظف في قسم الصيانة؟", language="ar")
    assert "لم أتعرّف على القسم" in answer
    assert "إجمالي المسجلين" not in answer


def test_inactive_count_answers_terminated_not_active(seeded):
    answer = queries.answer_structured_hr_query("كام موظف غير نشط؟", language="ar")
    assert "غير النشطين" in answer
    assert ": 0" in answer
    assert "النشطين حاليًا: 3" not in answer


def test_historical_dated_count_is_declined_not_answered_with_today(seeded):
    for question in (
        "كام موظف كان شغال في 2020؟",
        "how many employees did we have in 2019?",
        "كم عدد الموظفين بتاريخ 2020-01-15؟",
    ):
        assert not queries.is_employee_count_request(question)
        assert not queries.is_structured_hr_query(question)


def test_yesterday_absence_routes_to_yesterday_day_mode(seeded):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    answer = queries.answer_structured_hr_query("مين غايب امبارح؟", language="ar")
    assert yesterday in answer
    assert "لا يمكن الجزم بغياب من لا سجل له" in answer


def test_tomorrow_absence_routes_to_tomorrow_day_mode(seeded):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    answer = queries.answer_structured_hr_query("مين غايب بكرة؟", language="ar")
    assert tomorrow in answer


def test_negated_absence_is_declined_not_answered_with_flagged_rows(seeded):
    for question in (
        "مين مش غايب النهارده؟",
        "مين حضر النهارده؟",
        "who is not absent today?",
        "今天谁没有缺席？",
    ):
        assert not queries.is_attendance_analysis_request(question)
        assert not queries.is_structured_hr_query(question)


def test_unknown_employee_target_in_analysis_is_honest_not_found(seeded):
    answer = queries.answer_structured_hr_query("حلل حضور MY-9999 النهارده", language="ar")
    assert "لم يتم العثور على الموظف المحدد" in answer
    assert "MY-0001" not in answer  # never substitute another employee


def test_known_employee_target_filters_the_analysis(seeded):
    answer = queries.answer_structured_hr_query("حلل حضور MY-0001 النهارده", language="ar")
    assert "MY-0001" in answer
    assert "MY-0002" not in answer


def test_guide_marker_does_not_hijack_leave_balance_read(seeded):
    question = "can i check MY-0001 leave balance?"
    assert not is_usage_guide_request(question)
    assert queries.is_structured_hr_query(question)
def test_scoped_count_never_treated_as_unknown_employee_name(seeded):
    answer = queries.answer_structured_hr_query("كام موظف اتأخر النهارده؟", language="ar")
    assert "لم يتم العثور على الموظف المحدد" not in answer
    assert "تحليل حضور" in answer
def test_department_name_scopes_count_without_literal_marker(seeded):
    answer = queries.answer_structured_hr_query("كام موظف في الاستقبال؟", language="ar")
    assert "قسم الاستقبال" in answer
    assert ": 3" in answer
    assert "إجمالي المسجلين" not in answer


def test_en_not_active_count_answers_inactive(seeded):
    answer = queries.answer_structured_hr_query("how many employees are not active?", language="en")
    assert "Inactive employees" in answer
    assert ": 0" in answer
    assert "Active employees right now: 3" not in answer


def test_zh_inactive_count_variant(seeded):
    answer = queries.answer_structured_hr_query("多少员工不在职？", language="zh")
    assert "非在职" in answer
    assert "0" in answer


def test_combined_inactive_department_count_keeps_both_scopes(seeded):
    answer = queries.answer_structured_hr_query("كام موظف غير نشط في قسم الاستقبال؟", language="ar")
    assert "غير النشطين في قسم الاستقبال" in answer
    assert "عدد الموظفين النشطين في قسم" not in answer
    answer_en = queries.answer_structured_hr_query(
        "how many inactive employees in الاستقبال?", language="en")
    assert "Inactive employees in the الاستقبال department" in answer_en


def test_relative_dated_count_is_declined(seeded):
    for question in (
        "how many employees worked yesterday?",
        "كام موظف كان شغال امبارح؟",
        "多少员工昨天在职？",
        "كام موظف كان شغال بتاريخ 2020-01-15؟",
    ):
        assert not queries.is_employee_count_request(question)
        assert not queries.is_structured_hr_query(question)


def test_fallback_dated_count_states_explicit_uncertainty():
    from app.ai_assistant import local_fallback_answer
    for question, lang in (
        ("how many employees did we have on 2020-01-01?", "en"),
        ("كم عدد الموظفين بتاريخ 2020-01-15؟", "ar"),
        ("2020年1月1日我们有多少员工？", "zh"),
    ):
        answer = local_fallback_answer(question, {"metrics": {"active_employees": 3}}, language=lang)
        assert "3" not in answer.replace("2020", "").replace("01", "").replace("15", "")
        assert {"ar": "لا أستطيع", "en": "cannot determine", "zh": "无法"}[lang] in answer


def test_unknown_named_target_in_analysis_is_not_found(seeded):
    answer = queries.answer_structured_hr_query("حلل حضور الموظف زيد النهارده", language="ar")
    assert "لم يتم العثور على الموظف المحدد" in answer
    assert "MY-0001" not in answer


def test_zh_want_to_view_balance_is_a_read_not_a_guide(seeded):
    question = "我想查看MY-0001的请假余额"
    assert not is_usage_guide_request(question) or queries.is_leave_balance_request(question)
    answer = queries.answer_structured_hr_query(question, language="zh")
    assert "添加请假" not in answer  # not the create-leave guide


def test_late_count_answered_when_clock_data_exists(seeded):
    from datetime import datetime
    with seeded.begin() as db:
        row = db.scalars(select(AttendanceDailyRecord)).one()
        row.scheduled_start_at = datetime(2026, 1, 1, 9, 0, 0)
        row.first_clock_in = datetime(2026, 1, 1, 9, 30, 0)
        db.commit()
    answer = queries.answer_structured_hr_query("كام موظف اتأخر النهارده؟", language="ar")
    assert "المتأخرون عن موعد بداية الوردية: 1." in answer


def test_late_count_declines_explicitly_without_clock_data(seeded):
    answer = queries.answer_structured_hr_query("كام موظف اتأخر النهارده؟", language="ar")
    assert "لا يمكن تحديد عدد المتأخرين" in answer
