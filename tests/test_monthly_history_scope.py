"""Monthly evidence keeps former employees; custom active-roster defaults stay."""
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import create_engine,select
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Employee, PayrollRun, PayrollItem, AttendanceDailyRecord
from app import report_builder as r
from tests.test_monthly_report import _seed

@pytest.fixture
def history():
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            _,_,active,left=_seed(db)
            left.status='archived'
            db.add(Employee(code='MY-NONE',full_name='No period evidence',department_id=active.department_id,position='Synthetic',hire_date=date(2024,1,1),status='archived',basic_salary=Decimal(1),daily_hours=Decimal(8)))
            db.commit()
            yield db,active.id,left.id
    finally:engine.dispose()

@pytest.mark.parametrize('option',[2,3,4])
def test_monthly_approved_history_retains_departed_and_current_status(history,monkeypatch,option):
    db,active,left=history
    original=r.build_report;seen=[]
    def capture(db,selection):
        result=original(db,selection);seen.append((selection,result));return result
    monkeypatch.setattr(r,'build_report',capture)
    assert r.build_monthly_pdf(db,date(2026,10,1),{option,10,11},language='en').startswith(b'%PDF')
    selection,result=seen[0]
    rows=[row for g in result.groups for row in g['rows']]
    assert selection.employee_status=='all' and selection.approved_only
    assert {row['employee_id'] for row in rows}=={active,left}
    assert {row['employment_status'] for row in rows}=={'نشط','غادر'}
    assert ('employment_status','حالة الموظف الحالية',False) in result.columns
    if option==2:
        assert result.totals['pay_net']==r._payroll_month_summary(db,date(2026,10,1),date(2026,10,31))['current']['net']==Decimal(12000)
    else:assert result.totals['att_present']==4

@pytest.mark.parametrize('kind',[r.REPORT_TYPE_PAYROLL,r.REPORT_TYPE_ATTENDANCE])
def test_custom_active_default_and_columns_unchanged(history,kind):
    db,active,left=history
    result=r.build_report(db,r.ReportSelection(report_type=kind,start_date=date(2026,10,1),end_date=date(2026,10,31),period_choice='custom'))
    rows=[row for g in result.groups for row in g['rows']]
    assert {row['employee_id'] for row in rows}=={active}
    assert all('employment_status' not in row for row in rows)
    assert all(key!='employment_status' for key,_,_ in result.columns)

@pytest.mark.parametrize('option',[5,6])
def test_monthly_record_sections_retain_departed_without_changing_state_filter(history,monkeypatch,option):
    from datetime import datetime
    from app.models import AdvanceRequest,StaffAction
    db,active,left=history
    for employee in (active,left):
        db.add(AdvanceRequest(employee_id=employee,request_date=date(2026,10,5),payroll_month=date(2026,10,1),amount=Decimal(10),salary_basis=Decimal(100),reason='Synthetic',created_by='test',status='pending'))
        db.add(StaffAction(employee_id=employee,kind='reward',category='other',title='Synthetic action',reason='Synthetic',created_by='test',created_at=datetime(2026,10,5),status='draft'))
    db.commit()
    original=r.build_report;seen=[]
    def capture(db,selection):
        result=original(db,selection);seen.append((selection,result));return result
    monkeypatch.setattr(r,'build_report',capture)
    r.build_monthly_pdf(db,date(2026,10,1),{option})
    selection,result=seen[0];rows=[row for g in result.groups for row in g['rows']]
    assert selection.employee_status=='all' and not selection.approved_only
    assert {row['employee_id'] for row in rows}=={active,left}
    assert {row['employment_status'] for row in rows}=={'نشط','غادر'}
    assert {row['status'] for row in rows}==({'معلقة'} if option==5 else {'مسودة'})

@pytest.mark.parametrize('option',[2,3,4])
def test_monthly_keeps_approved_history_excludes_unapproved_and_other_period(history,monkeypatch,option):
    db,active,left=history
    # Current approved data remains; an overlapping draft must not inflate it.
    run=PayrollRun(period_start=date(2026,10,2),period_end=date(2026,10,31),pay_date=date(2026,11,1),status='draft',created_by='test')
    db.add(run);db.flush()
    employee=db.get(Employee,left)
    db.add(PayrollItem(payroll_run_id=run.id,employee_id=left,employee_name_snapshot=employee.full_name,employee_code_snapshot=employee.code,department_name_snapshot='Synthetic',position_snapshot='Synthetic',salary_basis=Decimal(999),base_salary=Decimal(999),daily_hours=Decimal(8),net_pay=Decimal(999)))
    db.add(AttendanceDailyRecord(employee_id=left,work_date=date(2026,10,6),status='needs_review',worked_minutes=480))
    db.add(AttendanceDailyRecord(employee_id=left,work_date=date(2026,9,6),status='approved',worked_minutes=480))
    db.commit()
    original=r.build_report;seen=[]
    def capture(db,selection):
        result=original(db,selection);seen.append(result);return result
    monkeypatch.setattr(r,'build_report',capture)
    r.build_monthly_pdf(db,date(2026,10,1),{option,10})
    result=seen[0]
    if option==2:
        assert result.totals['pay_net']==Decimal(12000)
        assert dict(r._monthly_six_month_series(db,date(2026,10,1),r.REPORT_TYPE_PAYROLL))['10/2026']==12000
    else:
        assert result.totals['att_present']==4
        assert result.comparison['items'][0][2]==1
        assert dict(r._monthly_six_month_series(db,date(2026,10,1),r.REPORT_TYPE_ATTENDANCE))['10/2026']==4

@pytest.fixture
def paragraphs(monkeypatch):
    import reportlab.platypus as p
    original=p.Paragraph;texts=[]
    def record(text,*args,**kwargs):
        texts.append(text);return original(text,*args,**kwargs)
    monkeypatch.setattr(p,'Paragraph',record)
    return texts

@pytest.mark.parametrize('language',['ar','en','zh'])
def test_approved_only_content_never_claims_zero_unresolved(history,paragraphs,language):
    db,active,left=history
    db.add(AttendanceDailyRecord(employee_id=left,work_date=date(2026,10,6),status='needs_review',worked_minutes=480));db.commit()
    r.build_monthly_pdf(db,date(2026,10,1),{3,10},language=language)
    # Capture exact pre-shaping source text via the renderer's translation seam.
    from app.i18n import EN,ZH
    note={'ar':'هذا القسم يعرض المعتمد فقط ولا يقيس السجلات غير المحسومة.','en':EN['هذا القسم يعرض المعتمد فقط ولا يقيس السجلات غير المحسومة.'],'zh':ZH['هذا القسم يعرض المعتمد فقط ولا يقيس السجلات غير المحسومة.']}[language]
    if language=='en':
        joined=' '.join(paragraphs)
        assert note in joined and 'Unresolved days' not in joined
        assert 'Previous-month comparison is unavailable' in joined
    else:assert paragraphs

@pytest.mark.parametrize('option',[2,3,4,5,6,7,8,9])
def test_requested_comparison_is_rendered_or_explicitly_unavailable(history,paragraphs,option):
    db,_,_=history
    r.build_monthly_pdf(db,date(2026,10,1),{option,10},language='en',aggregate_leave=(option==7))
    joined=' '.join(paragraphs)
    assert any(phrase in joined for phrase in ('Previous-month comparison is unavailable','no separate previous-month comparison','12,000','Previous Month Comparison'))
    if option==2:
        assert '12,000' in joined
        # Standard comparison is present, beyond the old continuing-worker block.
        assert any('09/2026' in text for text in paragraphs)

@pytest.mark.parametrize('status',['draft','reopened'])
def test_missing_approved_payroll_is_unavailable_not_zero(history,paragraphs,status):
    db,_,_=history
    for run in db.scalars(select(PayrollRun).where(PayrollRun.period_start==date(2026,10,1))):run.status=status
    db.commit()
    r.build_monthly_pdf(db,date(2026,10,1),{1,8,10},language='en')
    assert any('values are unavailable, not zero' in t for t in paragraphs)
    assert r._payroll_month_summary(db,date(2026,10,1),date(2026,10,31))['current_approved_runs']==0
    assert dict(r._monthly_six_month_series(db,date(2026,10,1),r.REPORT_TYPE_PAYROLL))['10/2026'] is None


def test_actual_zero_approved_payroll_does_not_claim_missing(history,paragraphs):
    db,_,_=history
    run=db.scalar(select(PayrollRun).where(PayrollRun.period_start==date(2026,10,1)))
    for item in db.scalars(select(PayrollItem).where(PayrollItem.payroll_run_id==run.id)):item.net_pay=Decimal(0)
    db.commit()
    r.build_monthly_pdf(db,date(2026,10,1),{1,8},language='en')
    assert not any('values are unavailable, not zero' in t or 'No approved data' in t for t in paragraphs)
    assert dict(r._monthly_six_month_series(db,date(2026,10,1),r.REPORT_TYPE_PAYROLL))['10/2026']==0


@pytest.mark.parametrize('language',['ar','en','zh'])
def test_localized_render_inputs_include_truth_labels(history,monkeypatch,language):
    from app import reports
    db,_,_=history;seen=[];original=reports._pdf_cell
    def capture(value):seen.append(str(value));return original(value)
    monkeypatch.setattr(reports,'_pdf_cell',capture)
    for run in db.scalars(select(PayrollRun)):run.status='draft'
    db.commit()
    r.build_monthly_pdf(db,date(2026,10,1),{1,2,3,10},language=language)
    for source in ['هذا القسم يعرض المعتمد فقط ولا يقيس السجلات غير المحسومة.','لا توجد مسيرة رواتب معتمدة أو مغلقة لهذا الشهر؛ القيم غير متاحة وليست صفرًا.','لا توجد بنود رواتب معتمدة أو مغلقة لهذا الشهر؛ لا تُفترض قيم صفرية للمقارنة.']:
        # Chinese renderer calls the source seam on split non-CJK segments.
        if language!='zh':assert any(r._L(source,language) in text for text in seen)
        else:assert r._L(source,language)!=source


def test_actual_migrated_pg_history_truth_comparison_and_readonly(monkeypatch):
    from app.db import engine as configured
    from sqlalchemy import text
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    import uuid
    if configured.dialect.name!='postgresql':pytest.skip('Actual migrated PostgreSQL required')
    schema='monthly_history_'+uuid.uuid4().hex[:12]
    with configured.begin() as c:c.execute(text('CREATE SCHEMA '+schema))
    engine=create_engine(configured.url,connect_args={'options':'-csearch_path='+schema})
    try:
        cfg=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
        with engine.begin() as c:cfg.attributes['connection']=c;command.upgrade(cfg,'head')
        with Session(engine) as db:
            _,_,active,left=_seed(db);left.status='archived'
            db.add(AttendanceDailyRecord(employee_id=left.id,work_date=date(2026,9,6),status='approved',worked_minutes=480));db.commit()
            original=r.build_report;seen=[]
            def capture(db,selection):
                result=original(db,selection);seen.append(result);return result
            monkeypatch.setattr(r,'build_report',capture)
            tables=['employees','payroll_runs','payroll_items','attendance_daily_records','audit_logs']
            def state():return {table:db.execute(text('SELECT to_jsonb(t) FROM '+table+' t ORDER BY id')).scalars().all() for table in tables}
            before=state()
            for language in ('ar','en','zh'):
                assert r.build_monthly_pdf(db,date(2026,10,1),{1,2,3,4,8,9,10,11},language).startswith(b'%PDF')
            assert state()==before and not db.new and not db.dirty and not db.deleted
            assert all({row['employment_status'] for group in result.groups for row in group['rows']}=={'نشط','غادر'} for result in seen)
            assert all(label!='أيام غير محسومة' for result in seen for label,value in result.summary)
    finally:
        engine.dispose()
        with configured.begin() as c:c.execute(text('DROP SCHEMA '+schema+' CASCADE'))


def test_attendance_chart_empty_count_is_not_claim_of_missing_or_complete_records(history,paragraphs):
    from sqlalchemy import delete
    db,_,_=history
    db.execute(delete(AttendanceDailyRecord));db.execute(delete(PayrollItem));db.execute(delete(PayrollRun));db.commit()
    r.build_monthly_pdf(db,date(2026,10,1),{9,10},language='en')
    assert any("not proof that the month's register is complete" in text for text in paragraphs)
    assert not any('No approved data' in text for text in paragraphs)
    assert any('no approved run' in text.lower() for text in paragraphs)
