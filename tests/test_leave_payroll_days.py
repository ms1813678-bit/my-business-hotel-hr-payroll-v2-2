"""Live migrated PostgreSQL role/current-source day-only reader."""
from datetime import date
import pytest
from sqlalchemy import select,func
from app.leave_payroll_days import read_payroll_days
from app.models import AdminAccount,EmployeeServicePeriod,LeaveRegisterEntry,LeaveBalanceTransaction,PayrollItem,AuditLog,LeaveMonthNote
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_source_reconciliation import setup_source


@pytest.mark.parametrize('role',['system_admin','finance','assistant_manager'])
def test_day_read_role_and_no_effect(history_context,role):
    db,actor,auth,_=history_context
    db,e,_=setup_source(history_context)
    actor.role=role;db.flush()
    before=tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,PayrollItem,LeaveRegisterEntry,AuditLog))
    call=dict(employee_id=e.id,year=2026,month=2,**auth)
    if role=='assistant_manager':
        with pytest.raises(ValueError):read_payroll_days(db,**call)
    else:
        result=read_payroll_days(db,**call)
        assert result['ready'] and result['evidence'].present_days==3 and result['evidence'].codes['A']==1
    assert tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,PayrollItem,LeaveRegisterEntry,AuditLog))==before


@pytest.mark.parametrize('change',['unknown','overlap','missing_service','epoch','revoked','role'])
def test_day_evidence_blockers_and_current_authority(history_context,change):
    db,actor,auth,_=history_context
    db,e,_=setup_source(history_context)
    call=dict(employee_id=e.id,year=2026,month=2,**auth)
    if change=='unknown':db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id)).code=None
    elif change=='overlap':db.add(EmployeeServicePeriod(employee_id=e.id,start_date=date(2026,2,28),end_date=date(2026,2,28)))
    elif change=='missing_service':
        for p in db.scalars(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==e.id)):db.delete(p)
    elif change=='epoch':call['expected_epoch']='stale'
    elif change=='revoked':actor.active=False
    elif change=='role':actor.role='assistant_manager'
    db.flush()
    if change in {'epoch','revoked','role'}:
        with pytest.raises(ValueError):read_payroll_days(db,**call)
    else:assert not read_payroll_days(db,**call)['ready']


def test_source_reference_binds_exact_days_and_notes(history_context):
    db,actor,auth,_=history_context
    db,e,_=setup_source(history_context)
    call=dict(employee_id=e.id,year=2026,month=2,**auth)
    first=read_payroll_days(db,**call)
    rows=db.scalars(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,
        LeaveRegisterEntry.work_date>=date(2026,2,1),LeaveRegisterEntry.work_date<=date(2026,2,28)).order_by(LeaveRegisterEntry.work_date)).all()
    a=next(r for r in rows if r.code=='A');p=next(r for r in rows if r.code=='')
    a.code,p.code=p.code,a.code;db.flush()
    swapped=read_payroll_days(db,**call)
    assert swapped['evidence']==first['evidence']
    assert swapped['digest']!=first['digest']
    a.revision+=1;db.flush()
    revised=read_payroll_days(db,**call)
    assert revised['digest']!=swapped['digest']
    db.add(LeaveMonthNote(employee_id=e.id,year=2026,month=2,notes='synthetic preserved note',recorded_by=actor.username));db.flush()
    noted=read_payroll_days(db,**call)
    assert 'synthetic preserved note' in noted['evidence'].notes
    assert noted['digest']!=revised['digest']
