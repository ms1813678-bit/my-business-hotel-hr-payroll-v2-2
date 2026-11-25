"""Employee name selector (literal LIKE, deterministic bound, truncation) and smart-scan wording.
Runs against in-memory SQLite like test_ai_hr_queries.py; PostgreSQL ILIKE was not exercised here."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.ai_hr_queries as q
import app.ai_smart_scan as scan
from app.db import Base
from datetime import datetime

from app.models import (AdvanceRequest, AttendanceDailyRecord, Department, Employee, LeaveBalanceTransaction,
                        LeaveType, PayrollItem, PayrollRun, StaffAction)


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(q, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _add(factory, names):
    with factory() as db:
        dept = Department(name="Reception", active=True)
        db.add(dept)
        db.flush()
        for i, name in enumerate(names, start=1):
            db.add(Employee(code=f"MY-{i:04d}", full_name=name, department_id=dept.id, position="P",
                            hire_date=date(2026, 1, 1), status="active",
                            basic_salary=Decimal("1"), daily_hours=Decimal("8")))
        db.commit()


def _names(factory, question):
    with factory() as db:
        rows, truncated = q._find_employees_bounded(db, question)
        return [r.full_name for r in rows], truncated


def test_like_literal_escapes_backslash_first():
    assert q._like_literal("a_b") == "a\\_b"
    assert q._like_literal("50%") == "50\\%"
    assert q._like_literal("a\\b") == "a\\\\b"
    assert q._like_literal("\\%_") == "\\\\\\%\\_"
    assert q._like_literal("café 日本") == "café 日本"


def test_compiled_sql_has_escape_and_bound_pattern():
    stmt = select(Employee).where(Employee.full_name.ilike("%50\\%%", escape=q._LIKE_ESCAPE))
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ILIKE" in sql and "ESCAPE" in sql and "50" not in sql  # value is a bind, not inlined


def test_literal_underscore_and_percent_are_not_wildcards(db_factory):
    _add(db_factory, ["A_B", "AxB", "A%B", "AxxB", "A\\B"])
    assert _names(db_factory, "الموظف A_B") == (["A_B"], False)
    assert _names(db_factory, "الموظف A%B") == (["A%B"], False)
    assert _names(db_factory, "الموظف A\\B") == (["A\\B"], False)


def test_before_the_fix_the_wildcards_matched_other_people(db_factory):
    """Documents the defect: the old unescaped pattern matched AxB for 'A_B' and everyone for '%%'."""
    _add(db_factory, ["A_B", "AxB", "other person"])
    with db_factory() as db:
        old = db.scalars(select(Employee).where(Employee.full_name.ilike("%A_B%"))).all()
        assert sorted(e.full_name for e in old) == ["A_B", "AxB"]
    assert _names(db_factory, "الموظف A_B")[0] == ["A_B"]
    assert _names(db_factory, "الموظف %%")[0] == []


def test_substring_rule_unchanged_and_two_people_sharing_a_partial_name(db_factory):
    _add(db_factory, ["Omar Hassan", "Omar Said", "Nour Ali"])
    assert _names(db_factory, "الموظف Omar") == (["Omar Hassan", "Omar Said"], False)
    assert _names(db_factory, "الموظف omar hassan") == (["Omar Hassan"], False)  # case-insensitive as before
    assert _names(db_factory, "الموظف ali")[0] == ["Nour Ali"]  # still a substring match (policy unchanged)
    assert _names(db_factory, "الموظف zzz") == ([], False)
    assert _names(db_factory, "الموظف x") == ([], False)  # < 2 chars unchanged


def test_more_than_five_is_deterministic_and_flagged(db_factory):
    _add(db_factory, ["Sam 7", "Sam 3", "Sam 5", "Sam 1", "Sam 6", "Sam 2", "Sam 4"])
    rows, truncated = _names(db_factory, "الموظف Sam")
    assert rows == ["Sam 1", "Sam 2", "Sam 3", "Sam 4", "Sam 5"] and truncated is True
    assert _names(db_factory, "الموظف Sam") == (rows, True)  # repeatable
    _add_exact = ["Eve 1", "Eve 2", "Eve 3", "Eve 4", "Eve 5"]
    with db_factory() as db:
        dept = db.scalar(select(Department))
        for i, n in enumerate(_add_exact, start=100):
            db.add(Employee(code=f"MY-{i:04d}", full_name=n, department_id=dept.id, position="P",
                            hire_date=date(2026, 1, 1), status="active", basic_salary=Decimal("1"), daily_hours=Decimal("8")))
        db.commit()
    assert _names(db_factory, "الموظف Eve") == (_add_exact, False)  # exactly five is not truncated


def test_equal_names_tie_break_by_id(db_factory):
    _add(db_factory, ["Twin", "Twin"])
    with db_factory() as db:
        rows, _ = q._find_employees_bounded(db, "الموظف Twin")
        assert [r.code for r in rows] == ["MY-0001", "MY-0002"]


@pytest.mark.parametrize("text,code", [
    ("رصيد إجازة الموظف MY-0001", "MY-0001"), ("my0001", "MY-0001"), ("MY 0001", "MY-0001"),
    ("SEC-001", "SEC-001"), ("sec 001", "SEC-001"), ("请查询员工MY-0001的假期", "MY-0001"), ("员工 SEC-001 的资料", "SEC-001"),
])
def test_code_lookup_and_normalisation_unchanged(db_factory, text, code):
    with db_factory() as db:
        dept = Department(name="D", active=True)
        db.add(dept)
        db.flush()
        for c in ("MY-0001", "SEC-001"):
            db.add(Employee(code=c, full_name="Name " + c, department_id=dept.id, position="P", hire_date=date(2026, 1, 1),
                            status="active", basic_salary=Decimal("1"), daily_hours=Decimal("8")))
        db.commit()
    names, truncated = _names(db_factory, text)
    assert names == ["Name " + code] and truncated is False
    with db_factory() as db:
        assert [e.code for e in q._find_employees(db, text)] == [code]  # public wrapper keeps its list contract


def test_employee_report_shows_truncation_note_in_three_languages(db_factory):
    _add(db_factory, [f"Sam {i}" for i in range(1, 8)])
    report = q.build_employee_report("الموظف Sam")
    assert report["selection"]["truncated"] is True and len(report["items"]) == 5
    for lang, needle in (("ar", "أول 5 موظفين"), ("en", "first 5 matching employees"), ("zh", "前 5 名匹配员工")):
        assert needle in q.format_employee_report(report, lang)
    _add_one = q.build_employee_report("الموظف Sam 1")
    assert _add_one["selection"]["truncated"] is False
    assert "first 5" not in q.format_employee_report(_add_one, "en")
    assert q.build_employee_report("MY-0001")["selection"]["truncated"] is False


def test_smart_scan_labels_and_predicates(monkeypatch):
    ar = {r["key"]: r["label"] for r in scan.build_smart_scan("ar", lambda c: 0)}
    en = {r["key"]: r["label"] for r in scan.build_smart_scan("en", lambda c: 0)}
    zh = {r["key"]: r["label"] for r in scan.build_smart_scan("zh", lambda c: 0)}
    assert (ar["leave_drafts"], en["leave_drafts"], zh["leave_drafts"]) == (
        "مسودات إجازة بانتظار المراجعة", "Leave drafts awaiting review", "待审核的请假草稿")
    assert (ar["advance_pending"], en["advance_pending"], zh["advance_pending"]) == (
        "طلبات سلفة معلّقة أو معتمدة", "Advance requests pending or approved", "待处理或已批准的预支申请")
    # the counted predicate is unchanged: the labels describe exactly these statuses
    captured = {}
    monkeypatch.setattr(scan, "_count", lambda stmt: captured.update(stmt=stmt) or 0)
    for key in ("leave_drafts", "advance_pending"):
        captured.clear()
        next(c for c in scan._CHECKS if c["key"] == key)["count"]()
        sql = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
        assert ("'draft'" in sql) if key == "leave_drafts" else ("'pending'" in sql and "'approved'" in sql)
    assert not any("request" in en[k].lower() for k in ("leave_drafts",))  # no employee leave-request workflow implied



# ---------------- every name-selected report carries and shows the truncation warning ----------------
NEEDLES = {"ar": "أول 5 موظفين", "en": "first 5 matching employees", "zh": "前 5 名匹配员工"}


def _seed_everything(factory, count):
    """`count` employees "Sam 1".."Sam N" (reversed insert order), each with leave, advance, staff, payroll, attendance rows."""
    with factory() as db:
        dept = Department(name="Reception", active=True)
        lt = LeaveType(code="synthetic-selector-annual", name_ar="سنوية", name_en="Annual", is_paid=True, tracks_balance=True, active=True)
        db.add_all([dept, lt])
        db.flush()
        run = PayrollRun(period_start=date(2026, 11, 1), period_end=date(2026, 11, 30), pay_date=date(2026, 12, 1),
                         status="draft", created_by="t")
        db.add(run)
        db.flush()
        for i in range(count, 0, -1):
            e = Employee(code=f"MY-{i:04d}", full_name=f"Sam {i}", department_id=dept.id, position="P",
                         hire_date=date(2026, 1, 1), status="active", basic_salary=Decimal("1000"), daily_hours=Decimal("8"))
            db.add(e)
            db.flush()
            db.add(LeaveBalanceTransaction(employee_id=e.id, leave_type_id=lt.id, transaction_type="opening",
                                           days_delta=Decimal("5"), reason="r", created_by="t"))
            db.add(AdvanceRequest(employee_id=e.id, request_date=date(2026, 11, 16), payroll_month=date(2026, 11, 1),
                                  amount=Decimal("100"), advance_type="regular", salary_basis=Decimal("1000"),
                                  reason="r", status="approved", created_by="t"))
            db.add(StaffAction(employee_id=e.id, kind="sanction", category="c", title="t", is_financial=True, amount=Decimal("50"),
                               payroll_month=date(2026, 11, 1), reason="r", status="approved", created_by="t"))
            db.add(PayrollItem(payroll_run_id=run.id, employee_id=e.id, employee_code_snapshot=e.code,
                               employee_name_snapshot=e.full_name, department_name_snapshot="Reception",
                               position_snapshot="P", salary_basis=Decimal("1000"), base_salary=Decimal("1000"),
                               daily_hours=Decimal("8"), net_pay=Decimal("1000")))
            db.add(AttendanceDailyRecord(employee_id=e.id, work_date=date(2026, 11, 2),
                                         scheduled_start_at=datetime(2026, 11, 2, 9), scheduled_end_at=datetime(2026, 11, 2, 17),
                                         first_clock_in=datetime(2026, 11, 2, 9, 30), last_clock_out=None, status="needs_review"))
        db.commit()


def _attendance(question):
    return q.build_attendance_analysis(work_date=date(2026, 11, 2), month_start=None, question=question)


REPORTS = {
    "employee": (lambda t: q.build_employee_report(t), q.format_employee_report, lambda r: [i["name"] for i in r["items"]]),
    "payroll": (lambda t: q.build_payroll_report(t + " 2026-11"), q.format_payroll_report, lambda r: [i["name"] for i in r["items"]]),
    "attendance": (_attendance, q.format_attendance_analysis, lambda r: [i["employee_name"] for i in r["items"]]),
    "leave": (lambda t: q.build_leave_balance_report(t), q.format_leave_balance, lambda r: [e["name"] for e in r["employees"]]),
    "advance": (lambda t: q.build_advance_report(t), q.format_advance_report, lambda r: [i["name"] for i in r["items"]]),
    "staff": (lambda t: q.build_staff_actions_report(t + " 2026-11"), q.format_staff_actions, lambda r: [i["name"] for i in r["items"]]),
}
FIRST_FIVE = ["Sam 1", "Sam 2", "Sam 3", "Sam 4", "Sam 5"]


@pytest.mark.parametrize("kind", sorted(REPORTS))
def test_report_over_five_matches_is_marked_and_warned_in_three_languages(db_factory, kind):
    _seed_everything(db_factory, 7)
    build, fmt, names = REPORTS[kind]
    report = build("الموظف Sam")
    assert report["selection"] == {"limit": 5, "truncated": True}
    assert sorted(set(names(report))) == FIRST_FIVE  # only the deterministic first five, never "all matches"
    for lang, needle in NEEDLES.items():
        text = fmt(report, lang)
        assert needle in text, (kind, lang)


@pytest.mark.parametrize("kind", sorted(REPORTS))
def test_report_exactly_five_or_exact_code_has_no_warning(db_factory, kind):
    _seed_everything(db_factory, 5)
    build, fmt, names = REPORTS[kind]
    five = build("الموظف Sam")
    assert five["selection"]["truncated"] is False and len(set(names(five))) == 5
    one = build("MY-0003")
    assert one["selection"]["truncated"] is False and set(names(one)) == {"Sam 3"}
    for lang, needle in NEEDLES.items():
        assert needle not in fmt(five, lang) and needle not in fmt(one, lang)


@pytest.mark.parametrize("kind", ["payroll", "attendance", "advance", "staff", "leave", "employee"])
def test_no_match_never_becomes_all_employees(db_factory, kind):
    _seed_everything(db_factory, 7)
    build, fmt, names = REPORTS[kind]
    report = build("الموظف zzzz")
    assert names(report) == []
    assert not (report.get("selection") or {}).get("truncated")
    assert report.get("note") in {"note_employee_not_found", "note_specify_employee", "note_readonly_employee", None} or kind == "employee"


def test_unfiltered_reports_keep_all_rows_and_no_warning(db_factory):
    _seed_everything(db_factory, 7)
    adv = q.build_advance_report("سلف الموظفين")
    assert len({i["name"] for i in adv["items"]}) == 7 and adv["selection"]["truncated"] is False
    pay = q.build_payroll_report("مسير الرواتب 2026-11")
    assert len(pay["items"]) == 7 and pay["selection"]["truncated"] is False


def test_attendance_all_three_branches_propagate_the_flag(db_factory):
    _seed_everything(db_factory, 7)
    # (1) code branch cannot truncate; (2) definite named target "الموظف ..."; (3) filter requested via "موظف ..."
    assert _attendance("الموظف Sam")["selection"]["truncated"] is True
    assert _attendance("حضور موظف Sam")["selection"]["truncated"] is True
    assert _attendance("MY-0002")["selection"]["truncated"] is False
    text = q.format_attendance_analysis(_attendance("الموظف Sam"), "en")
    assert "first 5 matching employees" in text
    assert "Sam 6" not in text and "Sam 7" not in text  # counts cover the selected five only


@pytest.mark.parametrize('question', ['كام موظف اتأخر النهارده؟','كم موظف اتأخر اليوم؟','كل موظف حاضر','جميع موظف حاضر'])
def test_count_singular_is_not_named_employee_filter(question):
    assert q._employee_filter_requested(question) is False

@pytest.mark.parametrize('question', ['كم الموظف زيد','كام الحارس زيد','حلل حضور الموظف زيد النهارده','الموظف كام','MY-9999 كام موظف اتأخر','كام موظف MY-9999 اتأخر'])
def test_explicit_name_or_code_still_targets_and_never_falls_back(question):
    assert q._employee_filter_requested(question) is True
