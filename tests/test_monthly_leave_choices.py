from datetime import date
from decimal import Decimal
from sqlalchemy import create_engine,select
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Department,Employee,LeaveType,LeaveRecord
from app.report_builder import ReportSelection,REPORT_TYPE_LEAVE,build_report,_monthly_leave_aggregate,_flatten_groups,build_monthly_pdf
import pytest
@pytest.fixture
def fixture_db():
 e=create_engine('sqlite://');Base.metadata.create_all(e)
 with Session(e) as db:
  dep=Department(name='Testing');db.add(dep);db.flush();typ=LeaveType(code='A',name_ar='سنوية',name_en='Annual',name_zh='年假');db.add(typ);db.flush()
  for i,status in enumerate(['active','archived'],1):
   emp=Employee(code='TEST'+str(i),full_name='Test Employee '+str(i),department_id=dep.id,position='Testing',hire_date=date(2020,1,1),status=status,basic_salary=100,daily_hours=8);db.add(emp);db.flush()
   for state in ['confirmed','draft','cancelled']:
    db.add(LeaveRecord(employee_id=emp.id,leave_type_id=typ.id,start_date=date(2026,9,29),end_date=date(2026,10,3),calendar_days=5,charged_days=Decimal('5'),reason='Synthetic',status=state,created_by='test'))
   # Outside-period rows must not leak into either output.
   db.add(LeaveRecord(employee_id=emp.id,leave_type_id=typ.id,start_date=date(2026,8,1),end_date=date(2026,8,2),calendar_days=2,charged_days=Decimal('2'),reason='Outside',status='confirmed',created_by='test'))
  db.commit();yield db
 e.dispose()
def test_all_states_separate_historical_and_custom_unchanged(fixture_db):
 db=fixture_db;out=[]
 for state in ['confirmed','draft','cancelled']:
  sel=ReportSelection(report_type=REPORT_TYPE_LEAVE,start_date=date(2026,10,1),end_date=date(2026,10,31),period_choice='custom',employee_status='all',monthly_leave_clipping=True,monthly_leave_status=state,fields={'emp_name','emp_code'})
  result=build_report(db,sel);rows=[r for g in result.groups for r in g['rows']];assert len(rows)==2
  assert {r['status'] for r in rows}=={{'confirmed':'مؤكدة','draft':'مسودة','cancelled':'ملغاة'}[state]}
  assert {r['employment_status'] for r in rows}=={'نشط','غادر'}
  assert all(r['calendar_days']==3 and r['charged_days']==0 for r in rows)
  out.extend(rows)
 assert len(out)==6
 custom=build_report(db,ReportSelection(report_type=REPORT_TYPE_LEAVE,start_date=date(2026,10,1),end_date=date(2026,10,31),period_choice='custom'))
 rows=[r for g in custom.groups for r in g['rows']];assert len(rows)==3 and all(r['calendar_days']==5 for r in rows)
 assert all('employment_status' not in r for r in rows)
def test_aggregate_all_statuses_not_merged(fixture_db):
 rows=_monthly_leave_aggregate(fixture_db,date(2026,10,1),date(2026,10,31),all_statuses=True)
 assert len(rows)==3 and {r['status'] for r in rows}=={'confirmed','draft','cancelled'}
 assert all(r['count']==2 and r['days']==6 and r['charged']==0 for r in rows)
 # default helper behavior preserved, and custom flow never opts into new monthly field
 old=_monthly_leave_aggregate(fixture_db,date(2026,10,1),date(2026,10,31));assert len(old)==1 and old[0]['count']==2
@pytest.mark.parametrize('lang',['ar','en','zh'])
@pytest.mark.parametrize('aggregate',[False,True])
def test_pdf_and_read_only(fixture_db,lang,aggregate):
 db=fixture_db
 def snapshot():
  return {table.name: sorted((repr(tuple(row)) for row in db.execute(select(table)).all())) for table in Base.metadata.sorted_tables}
 before=snapshot()
 content=build_monthly_pdf(db,date(2026,10,1),{7},language=lang,aggregate_leave=aggregate);assert content.startswith(b'%PDF')
 after=snapshot();assert before==after
 assert not db.new and not db.dirty and not db.deleted

def test_monthly_pdf_routes_all_statuses_and_period_employees(fixture_db, monkeypatch):
 import app.report_builder as module
 original=module.build_report
 observed=[]
 def capture(db, selection):
  observed.append((selection.monthly_leave_status, selection.employee_status))
  return original(db, selection)
 monkeypatch.setattr(module, 'build_report', capture)
 build_monthly_pdf(fixture_db,date(2026,10,1),{7},language='en')
 assert observed==[(state,'all') for state in ('confirmed','draft','cancelled')]

def test_charge_counted_only_in_start_month(fixture_db):
 rows=_monthly_leave_aggregate(fixture_db,date(2026,9,1),date(2026,9,30),all_statuses=True)
 assert len(rows)==3
 assert all(row['count']==2 and row['days']==4 and row['charged']==10 for row in rows)

@pytest.mark.parametrize('lang, label', [('ar','سنوية'),('en','Annual'),('zh','年假')])
@pytest.mark.parametrize('aggregate', [False,True])
def test_actual_pdf_paragraphs_choose_stored_locale_leave_type(fixture_db, monkeypatch, lang, label, aggregate):
 import reportlab.platypus
 original=reportlab.platypus.Paragraph
 seen=[]
 def capture(text, *args, **kwargs):
  seen.append(text)
  return original(text,*args,**kwargs)
 monkeypatch.setattr(reportlab.platypus,'Paragraph',capture)
 assert build_monthly_pdf(fixture_db,date(2026,10,1),{7},language=lang,aggregate_leave=aggregate).startswith(b'%PDF')
 # CJK/Latin mixed-font markup preserves the stored localized label in the actual PDF text flow.
 if lang!='ar':
  assert any(label in text for text in seen)
  assert all('سنوية' not in text for text in seen)
 rows=_monthly_leave_aggregate(fixture_db,date(2026,10,1),date(2026,10,31),all_statuses=True,language=lang)
 assert {row['leave_type'] for row in rows}=={label}

@pytest.mark.parametrize('lang,label',[('en','Leave type translation missing'),('zh','缺少休假类型翻译')])
def test_missing_type_translation_has_locale_marker_not_arabic(fixture_db,lang,label):
 typ=fixture_db.scalars(select(LeaveType)).one()
 setattr(typ,'name_'+lang,'')
 fixture_db.flush()
 rows=_monthly_leave_aggregate(fixture_db,date(2026,10,1),date(2026,10,31),all_statuses=True,language=lang)
 assert {r['leave_type'] for r in rows}=={label+' ['+str(typ.id)+']'}
 sel=ReportSelection(report_type=REPORT_TYPE_LEAVE,start_date=date(2026,10,1),end_date=date(2026,10,31),period_choice='custom',employee_status='all',monthly_leave_status='confirmed',monthly_leave_language=lang)
 result=build_report(fixture_db,sel)
 assert {r['leave_type'] for g in result.groups for r in g['rows']}=={label+' ['+str(typ.id)+']'}

def test_new_monthly_label_locale_parity():
 from app.i18n import EN,ZH
 keys=['حالة الموظف الحالية','نشط','غادر','كل حالات الإجازات حسب النوع والحالة — بدون بيانات الموظفين','أيام الخصم المسجلة','لا توجد إجازات في هذا الشهر.','كل من له إجازة خلال الفترة مع حالته الحالية','المسودة والملغاة معروضتان منفصلتين؛ أيام الخصم المسجلة ليست خصمًا منفذًا من الرصيد.','أيام كل شهر تُعرض في شهرها، وأيام الخصم تُحسب مرة واحدة في شهر بداية الإجازة','ترجمة نوع الإجازة غير متاحة','حالة موظف غير معروفة']
 for key in keys:
  assert key in EN and key in ZH
  assert not any('\u0600'<=ch<='\u06ff' for ch in EN[key]+ZH[key])

def test_section_summary_own_status_and_unknown_employment(fixture_db):
 from app.report_builder import LEAVE_STATUS_LABELS
 emp=fixture_db.scalars(select(Employee)).first()
 # Corrupt/legacy state is simulated in memory only, not stored past a DB constraint.
 from sqlalchemy.orm.attributes import set_committed_value
 set_committed_value(emp,'status','legacy-unknown')
 for state in ('confirmed','draft','cancelled'):
  sel=ReportSelection(report_type=REPORT_TYPE_LEAVE,start_date=date(2026,10,1),end_date=date(2026,10,31),period_choice='custom',employee_status='all',monthly_leave_status=state)
  result=build_report(fixture_db,sel)
  assert result.summary[1]==(LEAVE_STATUS_LABELS[state],2)
  values={r['employment_status'] for g in result.groups for r in g['rows']}
  assert 'حالة موظف غير معروفة' in values and 'legacy-unknown' not in values

def test_two_missing_types_remain_distinguishable(fixture_db):
 db=fixture_db
 first=db.scalars(select(LeaveType)).one();first.name_zh=''
 second=LeaveType(code='B',name_ar='أخرى',name_en='Other',name_zh='');db.add(second);db.flush()
 employee=db.scalars(select(Employee)).first()
 db.add(LeaveRecord(employee_id=employee.id,leave_type_id=second.id,start_date=date(2026,10,10),end_date=date(2026,10,10),calendar_days=1,charged_days=Decimal('1'),reason='Synthetic',status='draft',created_by='test'));db.flush()
 rows=_monthly_leave_aggregate(db,date(2026,10,1),date(2026,10,31),all_statuses=True,language='zh')
 labels={row['leave_type'] for row in rows}
 assert labels=={'缺少休假类型翻译 ['+str(typ.id)+']' for typ in (first,second)}


def test_start_month_detail_all_statuses_all_workers_exact_total(fixture_db):
 from decimal import localcontext, Inexact, Rounded
 db=fixture_db
 # Valid stored fractions expose the old float-total mismatch.
 records=db.scalars(select(LeaveRecord).where(LeaveRecord.start_date==date(2026,9,29)).order_by(LeaveRecord.id)).all()
 for record in records:
  record.charged_days=Decimal('1.1') if record.employee_id==records[0].employee_id else Decimal('2.2')
 db.flush()
 with localcontext() as context:
  context.prec=2;context.traps[Inexact]=True;context.traps[Rounded]=True
  for state in ('confirmed','draft','cancelled'):
   selection=ReportSelection(report_type=REPORT_TYPE_LEAVE,start_date=date(2026,9,1),end_date=date(2026,9,30),period_choice='custom',employee_status='all',monthly_leave_clipping=True,monthly_leave_status=state)
   result=build_report(db,selection)
   rows=[row for group in result.groups for row in group['rows']]
   assert len(rows)==2 and {row['employment_status'] for row in rows}=={'نشط','غادر'}
   assert all(row['calendar_days']==2 for row in rows)
   assert {row['charged_days'] for row in rows}=={Decimal('1.1'),Decimal('2.2')}
   assert isinstance(result.totals['charged_days'],Decimal)
   assert result.totals['charged_days']==Decimal('3.3')
  aggregate=_monthly_leave_aggregate(db,date(2026,9,1),date(2026,9,30),all_statuses=True)
  assert all(row['charged']==Decimal('3.3') for row in aggregate)
 custom=build_report(db,ReportSelection(report_type=REPORT_TYPE_LEAVE,start_date=date(2026,9,1),end_date=date(2026,9,30),period_choice='custom',employee_status='all'))
 assert isinstance(custom.totals['charged_days'],float)


def test_monthly_sum_ignores_ambient_precision_and_rejects_unsupported_span():
 from decimal import localcontext, Inexact, Rounded, Overflow, Underflow
 from app.report_builder import _monthly_leave_charged_total
 for trapped in (False,True):
  with localcontext() as context:
   context.prec=2;context.Emax=9;context.Emin=-9
   for signal in (Inexact,Rounded,Overflow,Underflow):context.traps[signal]=trapped
   assert _monthly_leave_charged_total([Decimal('1.000000000000000000000000000001'),Decimal('0.000000000000000000000000000002')])==Decimal('1.000000000000000000000000000003')
   assert _monthly_leave_charged_total([Decimal('1e10'),Decimal('1')])==Decimal('10000000001')
   assert _monthly_leave_charged_total([])==Decimal(0)
   with pytest.raises(ValueError,match='bound'):_monthly_leave_charged_total([Decimal('1e100000')])


@pytest.mark.parametrize('lang',['ar','en','zh'])
def test_aggregate_pdf_paragraphs_never_contain_employee_identity(fixture_db,monkeypatch,lang):
 import reportlab.platypus
 original=reportlab.platypus.Paragraph
 seen=[]
 def capture(text,*args,**kwargs):
  seen.append(text)
  return original(text,*args,**kwargs)
 monkeypatch.setattr(reportlab.platypus,'Paragraph',capture)
 assert build_monthly_pdf(fixture_db,date(2026,9,1),{7},language=lang,aggregate_leave=True).startswith(b'%PDF')
 text=' '.join(seen)
 for employee in fixture_db.scalars(select(Employee)).all():
  assert employee.full_name not in text and employee.code not in text
 assert 'employment_status' not in text
