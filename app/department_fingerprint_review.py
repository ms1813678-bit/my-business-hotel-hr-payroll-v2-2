"""Join source-bound department days with raw calendar-date punch evidence.

No first/last direction inference, overnight assignment, shift guessing, name
linking, auto-P/U or posting. Calendar-date grouping is labeled explicitly, not
a claim punches belong to that workday. Leave without a punch is not an issue.
A separate reviewed shift/effect contract must precede automatic registration.
"""
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from app.department_workbook import WorkbookReturnPreview
from app.fingerprint_evidence import FingerprintEvidence


@dataclass(frozen=True)
class DepartmentPunchDayReview:
    employee_id: int
    department_work_date: date
    department_code: str | None
    source_sheet: str
    source_cell: str
    calendar_date_occurrences: tuple[dict, ...]
    review_state: str
    issues: tuple[str, ...]
    unreadable_source_evidence: tuple = ()
    eligible_for_registration: bool = False


@dataclass(frozen=True)
class DepartmentFingerprintReview:
    department_sha256: str
    fingerprint_sha256: str
    days: tuple[DepartmentPunchDayReview, ...]
    issues: tuple[str, ...]
    unmatched_occurrences: tuple[dict, ...]
    establishes_attendance_or_balance: bool = False


def preview_department_fingerprint_review(*, department: WorkbookReturnPreview,
                                          fingerprint: FingerprintEvidence):
    if not isinstance(department, WorkbookReturnPreview) or not isinstance(fingerprint, FingerprintEvidence):
        raise TypeError('Source-bound returned workbook and raw fingerprint evidence required.')
    if not department.days:
        raise ValueError('Nonempty inspected department return required.')
    if department.issues or any(d.issue for d in department.days):
        raise ValueError('Returned department workbook needs source-integrity review first.')
    import re
    if not all(isinstance(d,str) and re.fullmatch('[0-9a-f]{64}',d) for d in (department.source_sha256,fingerprint.source_sha256)):
        raise ValueError('Explicit source digests required, not origin proof.')
    if len(department.days)>9300 or len(fingerprint.occurrences)>200000:
        raise ValueError('Bounded source review scope required.')
    keys = [(d.employee_id,d.work_date) for d in department.days]
    if len(keys)!=len(set(keys)) or any(type(i) is not int or i<1 or type(day) is not date for i,day in keys):
        raise ValueError('Unique resolved department employee-days required.')
    if any(d.code not in (None,'P','A','W','H','S','U') for d in department.days):
        raise ValueError('Supported explicit department day codes required.')
    if len(fingerprint.issues)>20000:
        raise ValueError('Bounded source issue review required.')
    unreadable_index = {}
    for issue in fingerprint.issues:
        if issue.get('reason') in {'invalid_time_value','invalid_or_missing_date','missing_day_columns',
                'invalid_row_column_count','missing_employee_data_row','invalid_employee_identity'}:
            key = (issue.get('employee_id'), issue.get('work_date'))
            unreadable_index.setdefault(key, []).append(issue)
    key_set = set(keys)
    by_day = {}; unmatched=[]
    unreadable_references = 0
    for event in fingerprint.occurrences:
        employee_id, timestamp = event.get('employee_id'), event.get('punch_at')
        if type(timestamp) is not datetime:
            raise ValueError('Explicit raw punch timestamp required.')
        if employee_id is None:
            unmatched.append(dict(event)); continue
        if type(employee_id) is not int or employee_id<1:
            raise ValueError('Resolved punch identity or unknown required.')
        key=employee_id,timestamp.date()
        by_day.setdefault(key,[]).append(dict(event))
        if key not in key_set: unmatched.append(dict(event))
    days=[]
    for item in department.days:
        punches=tuple(by_day.get((item.employee_id,item.work_date),()))
        issues=[]
        issue_keys = ((item.employee_id,item.work_date), (item.employee_id,None),
                      (None,item.work_date), (None,None))
        unreadable=tuple(issue for key in issue_keys for issue in unreadable_index.get(key, ()))
        unreadable_references += len(unreadable)
        if unreadable_references > 200000:
            raise ValueError('Bounded unreadable evidence review required.')
        if item.code is None:
            state='unknown_department_day'
        elif item.code=='P':
            state='presence_needs_shift_direction_review' if punches else 'presence_without_calendar_punch_needs_review'
        elif item.code in {'A','H','W','S'}:
            state='explicit_leave_with_calendar_punch_needs_review' if punches else 'explicit_leave_no_punch_required'
        elif item.code=='U':
            state='absence_with_calendar_punch_needs_review' if punches else 'explicit_absence_needs_source_review'
        else:
            raise ValueError('Explicit supported department day code required.')
        if unreadable:
            state='unreadable_fingerprint_evidence_needs_review'
            issues.append('unreadable_fingerprint_source_not_absence')
        if punches:
            issues.append('calendar_date_only_shift_and_direction_unresolved')
            times=[p['punch_at'] for p in punches]
            if len(set(times))!=len(times): issues.append('duplicate_occurrences_retained')
        days.append(DepartmentPunchDayReview(item.employee_id,item.work_date,item.code,item.sheet,item.cell,
            tuple(MappingProxyType(dict(p)) for p in punches),state,tuple(issues),unreadable))
    global_issues=tuple(sorted({i['reason'] for i in fingerprint.issues}))
    return DepartmentFingerprintReview(department.source_sha256,fingerprint.source_sha256,tuple(days),
        global_issues,tuple(MappingProxyType(dict(p)) for p in unmatched))
