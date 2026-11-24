"""Read-only current configured-window diagnostics, parsing original bytes here."""
from datetime import date,datetime,timedelta
from dataclasses import dataclass,field
from hashlib import sha256
import json
from sqlalchemy import select,text,inspect
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_policy_store import policy_read_guard
from app.models import Employee,ShiftPattern
from app.attendance_import import normalize
from app.fingerprint_evidence import parse_fingerprint_evidence
from app.shift_evidence_preview import ExplicitShiftWindow,preview_shift_occurrences


@dataclass(frozen=True)
class ConfiguredShiftComparison:
    employee_id:int
    employee_code:str
    employee_name:str
    preview:object
    source_digest:str
    issues:tuple
    posting_allowed:bool=field(default=False,init=False)


@bounded_workbook_operation
def preview_configured_shift_evidence(db,*,employee_id,work_date,selected_windows,fingerprint_bytes,fingerprint_filename,operational_parse=False,**identity):
    """Selections are (workdate, configured pattern id), not actual assignments.

    Current department/configuration only, not effective-dated historical roster.
    Does not call legacy first/last reconciliation or any attendance mutation.
    """
    if db.new or db.dirty or db.deleted:raise ValueError('Clean read-only transaction required.')
    policy_read_guard(db,**identity)
    if type(employee_id) is not int or employee_id<1 or type(work_date) is not date:raise ValueError('Explicit resolved employee/date required.')
    if not isinstance(selected_windows,(tuple,list)) or not 1<=len(selected_windows)<=100:raise ValueError('Bounded explicit configured selections required.')
    selections=[]
    for selection in selected_windows:
        if not isinstance(selection,(tuple,list)) or len(selection)!=2:raise ValueError('Date/pattern selection required.')
        day,pattern_id=selection
        if type(day) is not date or abs((day-work_date).days)>1 or type(pattern_id) is not int or pattern_id<1:raise ValueError('Exact adjacent date and configured id required.')
        if (day,pattern_id) in selections:raise ValueError('Duplicate selection refused.')
        selections.append((day,pattern_id))
    db.execute(text('LOCK TABLE employees,shift_patterns IN SHARE MODE'))
    policy_read_guard(db,**identity)
    employee=db.get(Employee,employee_id,populate_existing=True)
    if employee is None:raise ValueError('Employee unavailable.')
    employees=db.execute(select(Employee.id,Employee.code,Employee.fingerprint_id).order_by(Employee.id)).all()
    owners={}
    for item in employees:
        for alias in (item.code,item.fingerprint_id):
            if alias:owners.setdefault(normalize(alias),set()).add(item.id)
    for alias in (employee.code,employee.fingerprint_id):
        if alias and owners.get(normalize(alias))!={employee_id}:
            raise ValueError('Alias identity collision requires review.')
    # Recognize unique coworker aliases only to exclude unrelated evidence.
    # Neither coworker identity nor their rows are returned in this preview.
    aliases={alias:item.id for item in employees for alias in (item.code,item.fingerprint_id)
        if alias and owners.get(normalize(alias))=={item.id}}
    if type(operational_parse) is not bool:raise ValueError('Exact parser mode required.')
    parser=parse_fingerprint_evidence
    if operational_parse:
        from app.fingerprint_bounded_parse import parse_operational_fingerprint
        parser=parse_operational_fingerprint
    evidence=parser(fingerprint_bytes,filename=fingerprint_filename,verified_aliases=aliases)
    if any(i.get('reason')=='unknown_employee' and len(owners.get(normalize(i.get('value')),set()))>1 for i in evidence.issues):
        raise ValueError('Ambiguous source alias requires identity review.')
    windows=[];patterns=[]
    for day,pattern_id in selections:
        pattern=db.get(ShiftPattern,pattern_id,populate_existing=True)
        if pattern is None or not pattern.active or pattern.department_id!=employee.department_id:raise ValueError('Current active employee-department pattern required.')
        if pattern.start_time is None or pattern.end_time is None:raise ValueError('Complete configured schedule required.')
        start=datetime.combine(day,pattern.start_time);end=datetime.combine(day,pattern.end_time)
        if end<=start:end+=timedelta(days=1)
        windows.append(ExplicitShiftWindow(f'{day.isoformat()}:{pattern.id}',employee_id,day,start,end,
            pattern.match_before_minutes,pattern.match_after_minutes,f'current_configured_pattern:{pattern.id}'))
        patterns.append({c.key:str(getattr(pattern,c.key)) for c in inspect(ShiftPattern).columns})
    result=preview_shift_occurrences(employee_id=employee_id,work_date=work_date,windows=windows,fingerprint=evidence)
    facts=dict(preview_digest=result.source_digest,employee={key:str(getattr(employee,key)) for key in ('id','code','fingerprint_id','full_name','department_id')},
        patterns=patterns,alias_bindings=sorted(aliases.items()),restore_epoch=identity['expected_epoch'])
    digest=sha256(json.dumps(facts,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return ConfiguredShiftComparison(employee_id,employee.code,employee.full_name,result,digest,
        ('whole_file_span_not_selected_employee_coverage','current_configuration_not_historical_roster','explicit_selection_not_assignment','no_attendance_registration'))
