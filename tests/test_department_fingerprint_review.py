from datetime import date
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.department_fingerprint_review import preview_department_fingerprint_review
from app.department_workbook import ENTRY_SHEET, inspect_department_return
from app.fingerprint_evidence import parse_fingerprint_evidence
from tests.test_attendance_import import workbook_bytes
from tests.test_department_workbook import make_sample


def review(codes, punches):
    payload,manifest=make_sample()
    wb=load_workbook(BytesIO(payload)); employee_id=manifest.employees[0].employee_id
    for day,code in codes.items(): wb[ENTRY_SHEET].cell(7,day+2).value=code
    output=BytesIO();wb.save(output);wb.close()
    department=inspect_department_return(output.getvalue(),manifest)
    raw=workbook_bytes([('Att.log report',[
        ['AC-No.','Name','Date','Check Time'],
        *[['SYN','Synthetic',date(manifest.month_start.year,manifest.month_start.month,day),times] for day,times in punches.items()]
    ])])
    fingerprint=parse_fingerprint_evidence(raw,filename='synthetic.xlsx',verified_aliases={'SYN':employee_id})
    return preview_department_fingerprint_review(department=department,fingerprint=fingerprint)


def test_real_workbook_return_plus_raw_punches_stays_review_only():
    result=review({1:'P',2:'A',3:'W',4:'U'}, {1:'08:00 08:00 16:00',3:'00:05',5:'08:00'})
    days=result.days
    assert days[0].review_state=='presence_needs_shift_direction_review'
    assert len(days[0].calendar_date_occurrences)==3
    assert 'duplicate_occurrences_retained' in days[0].issues
    assert days[1].review_state=='explicit_leave_no_punch_required' and not days[1].issues
    assert days[2].review_state=='explicit_leave_with_calendar_punch_needs_review'
    assert days[3].review_state=='explicit_absence_needs_source_review'
    assert days[4].review_state=='unknown_department_day'
    assert all(not d.eligible_for_registration for d in days)
    assert not result.establishes_attendance_or_balance
    assert len(result.department_sha256)==len(result.fingerprint_sha256)==64


@pytest.mark.parametrize('code',['A','W','H','S'])
def test_explicit_leave_without_calendar_punch_is_not_automatic_discrepancy(code):
    result=review({1:code},{2:'08:00'})
    assert result.days[0].review_state=='explicit_leave_no_punch_required'
    assert result.days[0].issues==()


def test_midnight_punch_not_borrowed_as_previous_day_exit():
    result=review({1:'P',2:'P'},{1:'23:00',2:'07:00'})
    assert len(result.days[0].calendar_date_occurrences)==1
    assert result.days[0].calendar_date_occurrences[0]['punch_at'].day==1
    assert result.days[1].calendar_date_occurrences[0]['punch_at'].day==2
    assert all('direction_unresolved' in d.issues[0] for d in result.days[:2])


@pytest.mark.parametrize('cell',['zz','08:00 xx 16:00','25:00'])
def test_unreadable_fingerprint_on_leave_day_is_not_clean_no_punch(cell):
    result=review({1:'A'},{1:cell})
    day=result.days[0]
    assert day.review_state=='unreadable_fingerprint_evidence_needs_review'
    assert 'unreadable_fingerprint_source_not_absence' in day.issues
    assert day.unreadable_source_evidence[0]['row']==2


def test_forged_empty_preview_is_refused():
    from app.department_workbook import WorkbookReturnPreview
    from app.fingerprint_evidence import FingerprintEvidence
    with pytest.raises(ValueError,match='Nonempty'):
        preview_department_fingerprint_review(department=WorkbookReturnPreview('0'*64,(),()),
            fingerprint=FingerprintEvidence('1'*64,b'x',None,None,(),()))


def test_invalid_date_evidence_cannot_make_employee_leave_look_clean():
    from dataclasses import replace
    from types import MappingProxyType
    from app.department_workbook import WorkbookReturnPreview, ReturnedDayEvidence
    from app.fingerprint_evidence import FingerprintEvidence
    department=WorkbookReturnPreview('a'*64,(),(ReturnedDayEvidence(1,date(2026,11,1),'A','A',None,'Daily','C7'),))
    fingerprint=FingerprintEvidence('b'*64,b'synthetic',None,None,(),
        (MappingProxyType({'reason':'invalid_or_missing_date','employee_id':1,'work_date':None,'sheet':'Raw','row':2,'raw_time_value':'08:00'}),))
    result=preview_department_fingerprint_review(department=department,fingerprint=fingerprint)
    assert result.days[0].review_state=='unreadable_fingerprint_evidence_needs_review'
    assert result.days[0].unreadable_source_evidence[0]['raw_time_value']=='08:00'
    # An unreadable date for a different known employee does not alter this day.
    other=replace(fingerprint,issues=(MappingProxyType({**fingerprint.issues[0],'employee_id':2}),))
    clean=preview_department_fingerprint_review(department=department,fingerprint=other)
    assert clean.days[0].review_state=='explicit_leave_no_punch_required'
