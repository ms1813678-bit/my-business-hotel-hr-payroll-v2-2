"""Actual PostgreSQL ILIKE ESCAPE and bounded selector regression on migrated schema."""
from datetime import date
from pathlib import Path
import uuid
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from app.db import engine as configured_engine
from app.models import Department, Employee
from app import ai_hr_queries as q
from tests.test_ai_employee_selector_scan import _add, _names

@pytest.fixture
def pg_names(monkeypatch):
    if configured_engine.dialect.name != "postgresql":
        pytest.skip("Actual migrated PostgreSQL required")
    schema="selector_pg_"+uuid.uuid4().hex[:12]
    with configured_engine.begin() as c:c.execute(text(f'CREATE SCHEMA "{schema}"'))
    e=create_engine(configured_engine.url,connect_args={"options":"-csearch_path="+schema})
    try:
        cfg=Config(str(Path(__file__).resolve().parents[1]/"alembic.ini"))
        with e.begin() as c:
            cfg.attributes["connection"]=c;command.upgrade(cfg,"head")
        factory=sessionmaker(e,autoflush=False)
        monkeypatch.setattr(q,"SessionLocal",factory)
        yield factory
    finally:
        e.dispose()
        with configured_engine.begin() as c:c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

def test_actual_pg_literal_metacharacters_and_normalized_code(pg_names):
    _add(pg_names,["A_B","AxB","A%B","AxxB",r"A\B"])
    assert _names(pg_names,"الموظف A_B")==(["A_B"],False)
    assert _names(pg_names,"الموظف A%B")==(["A%B"],False)
    assert _names(pg_names,r"الموظف A\B")==([r"A\B"],False)
    assert _names(pg_names,"الموظف %%")==([],False)
    assert _names(pg_names,"请查询员工MY-0001的假期")==(["A_B"],False)
    with pg_names() as db:
        before=db.execute(text("SELECT to_jsonb(t) FROM employees t ORDER BY id")).scalars().all()
        q._find_employees_bounded(db,"الموظف A_B")
        assert db.execute(text("SELECT to_jsonb(t) FROM employees t ORDER BY id")).scalars().all()==before
        assert not db.new and not db.dirty and not db.deleted

def test_actual_pg_name_overflow_flag_and_repeatable_order(pg_names):
    _add(pg_names,["Sam 7","Sam 3","Sam 5","Sam 1","Sam 6","Sam 2","Sam 4"])
    assert _names(pg_names,"الموظف Sam")==(["Sam 1","Sam 2","Sam 3","Sam 4","Sam 5"],True)
    assert _names(pg_names,"الموظف sam")==_names(pg_names,"الموظف Sam")
    report=q.build_employee_report("الموظف Sam")
    assert report["selection"]["truncated"] and len(report["items"])==5
    for language in ("ar","en","zh"):
        assert q.format_employee_report(report,language)

@pytest.mark.parametrize("question",["الموظف zzzz","حضور موظف zzzz","MY-9999"])
def test_actual_pg_attendance_missing_target_never_broadens(pg_names, question):
    from tests.test_ai_employee_selector_scan import _seed_everything
    _seed_everything(pg_names,7)
    report=q.build_attendance_analysis(work_date=date(2026,11,2),month_start=None,question=question)
    assert report["items"]==[] and report["reviewed_rows"]==0 and report["note"]=="note_employee_not_found"

@pytest.mark.parametrize("kind",["employee","payroll","attendance","leave","advance","staff"])
def test_actual_pg_all_report_overflow_truth(pg_names,kind):
    from tests.test_ai_employee_selector_scan import _seed_everything,REPORTS,NEEDLES
    _seed_everything(pg_names,7)
    build,fmt,names=REPORTS[kind]
    report=build("الموظف Sam")
    assert report["selection"]=={"limit":5,"truncated":True}
    assert len(set(names(report)))==5
    for language,needle in NEEDLES.items():assert needle in fmt(report,language)

@pytest.mark.parametrize("question",["تقرير حضور الموظفين", "كل موظف في القسم", "حضور كل حارس في القسم", "حضور أي موظف في السجل", "تقرير الحضور"])
def test_actual_pg_general_register_not_misread_as_individual(pg_names,question):
    from tests.test_ai_employee_selector_scan import _seed_everything
    _seed_everything(pg_names,7)
    assert not q._employee_filter_requested(question)
    report=q.build_attendance_analysis(work_date=date(2026,11,2),month_start=None,question=question)
    assert report["reviewed_rows"]==7
    assert report["note"]!="note_employee_not_found"

@pytest.mark.parametrize("question",["حضور الموظف Sam اليوم", "حضور موظف zzzz", "حضور الحارس zzzz", "حضور حارس zzzz"])
def test_actual_pg_unresolved_name_text_asks_for_code_never_broadens(pg_names,question):
    from tests.test_ai_employee_selector_scan import _seed_everything
    _seed_everything(pg_names,7)
    assert q._employee_filter_requested(question)
    report=q.build_attendance_analysis(work_date=date(2026,11,2),month_start=None,question=question)
    assert report["items"]==[] and report["reviewed_rows"]==0
    for language,phrase in [("ar","الاسم وحده"),("en","name alone"),("zh","仅输入姓名")]:
        assert phrase in q.format_attendance_analysis(report,language)
