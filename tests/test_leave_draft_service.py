from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Employee, Department, LeaveType, LeaveRecord, AuditLog, LeaveBalanceTransaction, CAIRO_TZ
from app.leave_workflow import LeaveDraftError, create_leave_draft

@pytest.fixture
def draft_db():
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        department=Department(name='Synthetic');db.add(department);db.flush()
        employee=Employee(code='TEST',full_name='Synthetic',department_id=department.id,position='Testing',hire_date=date(2020,1,1),status='active',basic_salary=100,daily_hours=8)
        types=[LeaveType(code=code,name_ar=code,name_en=code,active=True) for code in ('annual','sick','casual')]
        db.add_all([employee,*types]);db.commit()
        yield db,employee,types
    engine.dispose()

def command(db,employee,typ,**kw):
    args=dict(employee_id=employee.id,leave_type_id=typ.id,start_date=date(2026,12,10),end_date=date(2026,12,11),charged_days=Decimal('2'),reason='  Synthetic reason  ',actor='test-admin')
    args.update(kw)
    return create_leave_draft(db,**args)

def test_record_creates_draft_audit_no_ledger(draft_db):
    db,e,types=draft_db;r=command(db,e,types[0])
    assert r.status=='draft' and r.reason=='Synthetic reason'
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==0
    audit=db.scalars(select(AuditLog)).one()
    assert (audit.actor,audit.action,audit.entity_id,audit.reason)==('test-admin','leave_record_created',str(r.id),'Synthetic reason')
    assert audit.new_value=='2026-12-10|2026-12-11|2'
    db.rollback()
    assert db.scalar(select(func.count()).select_from(LeaveRecord))==0
    assert db.scalar(select(func.count()).select_from(AuditLog))==0

@pytest.mark.parametrize('start',[date(2020,2,1),date(2026,12,10)])
def test_recording_has_no_request_notice_gate(draft_db,start):
    db,e,t=draft_db
    assert command(db,e,t[0],start_date=start,end_date=start,charged_days=Decimal('1')).status=='draft'

@pytest.mark.parametrize('which',[1,2])
def test_sick_casual_existing_exceptions(draft_db,which):
    db,e,t=draft_db
    assert command(db,e,t[which]).status=='draft'

@pytest.mark.parametrize('kw,code',[
    ({'charged_days':Decimal('NaN')},'dates'),({'charged_days':Decimal('0')},'dates'),
    ({'charged_days':Decimal('3')},'days'),({'end_date':date(2026,12,9)},'dates'),
    ({'reason':'  '},'dates'),({'employee_id':999},'employee'),({'leave_type_id':999},'employee')])
def test_existing_rejection_codes(draft_db,kw,code):
    db,e,t=draft_db
    with pytest.raises(LeaveDraftError) as err:command(db,e,t[0],**kw)
    assert err.value.code==code
    assert not db.new and not db.dirty

def test_overlap_and_cancelled_exception(draft_db):
    db,e,t=draft_db;r=command(db,e,t[0]);db.flush()
    with pytest.raises(LeaveDraftError) as err:command(db,e,t[0])
    assert err.value.code=='overlap'
    r.status='cancelled';db.flush()
    assert command(db,e,t[0]).status=='draft'

def test_ai_and_route_share_historical_recording_rules_and_keep_audits(draft_db):
    from app.ai_agent import execute_plan
    db,e,t=draft_db
    for n,source in enumerate(('service','ai')):
        past=date(2020,2,1+n)
        if source=='service':record=command(db,e,t[0],start_date=past,end_date=past,charged_days=Decimal('1'))
        else:
            result=execute_plan(dict(action='create_leave',employee_id=e.id,leave_type_id=t[0].id,start_date=past.isoformat(),end_date=past.isoformat(),charged_days='1',reason='Synthetic historical entry'),actor='ai-admin',db=db)
            record=db.get(LeaveRecord,result.params['record_id'])
        assert record.status=='draft' and record.start_date==past
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==0
    assert set(db.scalars(select(AuditLog.action)))=={'leave_record_created','ai_leave_record_created'}

@pytest.mark.parametrize('which',['employee','type'])
def test_inactive_employee_or_type_refused(draft_db,which):
    db,e,t=draft_db
    if which=='employee':e.status='archived'
    else:t[0].active=False
    db.flush()
    with pytest.raises(LeaveDraftError) as err:command(db,e,t[0])
    assert err.value.code=='employee'
    assert db.scalar(select(func.count()).select_from(LeaveRecord))==0


def test_request_only_notice_copy_is_absent():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    assert 'error == "notice"' not in (root/'app/templates/leave.html').read_text()
    assert 'Planned leave is normally requested 72' not in (root/'docs/PROJECT_SPEC.md').read_text()
