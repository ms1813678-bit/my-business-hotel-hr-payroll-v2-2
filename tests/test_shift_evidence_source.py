from datetime import date,time
from decimal import Decimal
from sqlalchemy import select,func,inspect
import pytest
from app.models import Employee,ShiftPattern,AuditLog,AttendanceDailyRecord
from app.shift_evidence_source import preview_configured_shift_evidence
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_department_workbook_store import context  # noqa


def setup_source(history_context):
    db,actor,auth,fact=history_context
    employee=db.get(Employee,fact.employee_id)
    pattern=ShiftPattern(department_id=employee.department_id,code='SYN-NIGHT',name='Synthetic night',start_time=time(23),end_time=time(7),match_before_minutes=0,match_after_minutes=0,active=True)
    db.add(pattern);db.flush()
    raw=b'ID,Date,Time\nSYN-HISTORY,2026-11-01,23:00 23:00\nSYN-HISTORY,2026-11-02,07:00\nUNKNOWN,2026-11-01,23:30\n'
    return db,auth,employee,pattern,dict(employee_id=employee.id,work_date=date(2026,11,1),selected_windows=((date(2026,11,1),pattern.id),),fingerprint_bytes=raw,fingerprint_filename='synthetic.csv',**auth)


def test_actual_parser_source_aliases_duplicates_unknown_and_no_writes(history_context):
    db,auth,employee,pattern,args=setup_source(history_context)
    def state():return tuple(tuple(getattr(row,c.key) for c in inspect(type(row)).columns) for row in (employee,pattern)),tuple(db.scalar(select(func.count()).select_from(m)) for m in (AuditLog,AttendanceDailyRecord))
    before=state();result=preview_configured_shift_evidence(db,**args)
    assert len(result.preview.occurrences)==3 and len(result.preview.unresolved_identity_occurrences)==1
    assert result.preview.occurrences[0]['raw_event']['source_row']==2
    assert not result.posting_allowed and state()==before
    pattern.name='Synthetic changed config';db.flush()
    assert result.source_digest!=preview_configured_shift_evidence(db,**args).source_digest


@pytest.mark.parametrize('change',['epoch','session','finance','otherdepartment','inactive','collision','missingmargin'])
def test_current_identity_configuration_refusals(history_context,change):
    db,auth,employee,pattern,args=setup_source(history_context)
    if change=='epoch':args['expected_epoch']='stale'
    elif change=='session':args['session_version']+=1
    elif change=='finance':history_context[1].role='finance'
    elif change=='inactive':pattern.active=False
    elif change=='missingmargin':pattern.match_before_minutes=None
    elif change=='otherdepartment':
        from app.models import Department
        other=Department(name='Synthetic different',active=True);db.add(other);db.flush();pattern.department_id=other.id
    elif change=='collision':
        other=Employee(code='SYN-OTHER',fingerprint_id=employee.code,full_name='Synthetic',department_id=employee.department_id,position='Synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8));db.add(other)
    db.flush()
    with pytest.raises(ValueError):preview_configured_shift_evidence(db,**args)


def test_actual_parser_known_coworker_not_laundered_into_unknown(history_context):
    db,auth,employee,pattern,args=setup_source(history_context)
    coworker=Employee(code='SYN-COWORKER',full_name='Synthetic private coworker',department_id=employee.department_id,position='Synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8))
    db.add(coworker);db.flush()
    args['fingerprint_bytes']+=b'SYN-COWORKER,2026-11-01,23:45\n'
    result=preview_configured_shift_evidence(db,**args)
    assert len(result.preview.occurrences)==3
    assert len(result.preview.unresolved_identity_occurrences)==1
    assert result.preview.unresolved_identity_occurrences[0]['raw_event']['raw_employee_code']=='UNKNOWN'
    assert 'SYN-COWORKER' not in str(result.preview)
    assert 'Synthetic private coworker' not in str(result)


def test_actual_parser_malformed_other_employee_time_retained_as_issue_not_abort(history_context):
    db,auth,employee,pattern,args=setup_source(history_context)
    args['fingerprint_bytes']+=b'UNKNOWN,2026-11-01,not-a-time\n'
    result=preview_configured_shift_evidence(db,**args)
    issues=result.preview.unreadable_evidence
    assert any(i.get('reason')=='invalid_time_value' and i.get('row')==5 for i in issues)


def test_actual_csv_coverage_diagnostics_and_unused_salary_not_digest(history_context):
    db,auth,employee,pattern,args=setup_source(history_context)
    result=preview_configured_shift_evidence(db,**args)
    assert result.preview.source_period_start==date(2026,11,1)
    assert result.preview.source_period_end==date(2026,11,2)
    assert result.preview.diagnostic_counts['total_occurrences']==4
    assert result.preview.diagnostic_counts['duplicate_source_issues']==1
    employee.basic_salary=Decimal(777);db.flush()
    assert result.source_digest==preview_configured_shift_evidence(db,**args).source_digest


@pytest.mark.parametrize('filename,raw',[('synthetic.xlsx',b'not-a-zip'),('synthetic.xls',b'not-an-ole-workbook'),('synthetic.xlsx',b'PK\x03\x04broken')])
def test_actual_corrupt_container_controlled_refusal_no_effect(history_context,filename,raw):
    db,auth,employee,pattern,args=setup_source(history_context)
    before=db.scalar(select(func.count()).select_from(AuditLog))
    with pytest.raises(ValueError):preview_configured_shift_evidence(db,**(args|dict(fingerprint_filename=filename,fingerprint_bytes=raw)))
    assert db.scalar(select(func.count()).select_from(AuditLog))==before


def test_actual_unknown_identity_diagnostic_typed_not_malformed_time(history_context):
    db,auth,employee,pattern,args=setup_source(history_context)
    args['fingerprint_bytes']+=b'UNKNOWN,2026-12-01,08:00\n'
    result=preview_configured_shift_evidence(db,**args)
    assert len(result.preview.unresolved_identity_occurrences)==1
    assert len(result.preview.identity_issues)==1
    assert result.preview.identity_issues[0]['work_date']==date(2026,11,1)
    assert not any(i['reason']=='unknown_employee' for i in result.preview.unreadable_evidence)
    assert result.preview.diagnostic_counts['out_of_range_issues']==1
    assert result.preview.diagnostic_counts['unknown_outside_windows']==1


def test_shared_coworker_alias_controlled_refusal_not_unknown_disclosure(history_context):
    db,auth,employee,pattern,args=setup_source(history_context)
    for code in ('SYN-SHARED','SYN-COWORKER-B'):
        db.add(Employee(code=code,fingerprint_id=('SYN-UNIQUE' if code=='SYN-SHARED' else 'SYN-SHARED'),full_name='Synthetic private',department_id=employee.department_id,position='Synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8)))
    db.flush();args['fingerprint_bytes']+=b'SYN-SHARED,2026-11-01,23:45\n'
    with pytest.raises(ValueError,match='Ambiguous source alias'):preview_configured_shift_evidence(db,**args)


def test_actual_corrupt_deflate_fixture_controlled_refusal(history_context):
    from pathlib import Path
    import zlib
    db,auth,employee,pattern,args=setup_source(history_context)
    raw=Path('tests/fixtures/synthetic/corrupt_deflate.xlsx').read_bytes()
    with pytest.raises(ValueError) as failure:
        preview_configured_shift_evidence(db,**(args|dict(fingerprint_filename='synthetic.xlsx',fingerprint_bytes=raw)))
    assert isinstance(failure.value.__cause__,zlib.error)
