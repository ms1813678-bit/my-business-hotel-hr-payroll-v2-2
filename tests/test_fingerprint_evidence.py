from datetime import date, datetime
from hashlib import sha256

import pytest

from app.attendance_import import parse_attendance_workbook
from app.fingerprint_evidence import parse_fingerprint_evidence
from tests.test_attendance_import import workbook_bytes


def payload():
    return workbook_bytes([('Att.log report',[
        ['AC-No.','Name','Date','Check Time'],
        ['FP-1','Synthetic employee',date(2026,11,1),'08:00 08:00 16:00'],
        ['FP-1','Synthetic employee',date(2026,11,2),'00:05 08:00 16:00'],
        ['UNKNOWN','Synthetic unmapped',date(2026,11,2),'08:30'],
    ])])


def test_original_bytes_duplicate_occurrences_and_unknown_identity_preserved():
    raw=payload(); result=parse_fingerprint_evidence(raw, filename='synthetic.xlsx', verified_aliases={'FP-1':1})
    assert result.source_sha256==sha256(raw).hexdigest() and result.original_bytes==raw
    assert len(result.occurrences)==7
    assert result.occurrences[0]['punch_at']==result.occurrences[1]['punch_at']==datetime(2026,11,1,8)
    assert result.occurrences[0]['event_index']!=result.occurrences[1]['event_index']
    assert result.occurrences[-1]['employee_id'] is None
    assert all(p['event_type']=='PUNCH' for p in result.occurrences)
    assert any(i['reason']=='duplicate_punch' for i in result.issues)
    assert any(i['reason']=='unknown_employee' for i in result.issues)
    # Existing legacy callers retain old behavior; no historical row rewrite.
    legacy=parse_attendance_workbook(raw,'synthetic.xlsx','fingerprint',{'FP-1':1})
    assert len(legacy['events'])==6


@pytest.mark.parametrize('aliases',[{}, {'FP':True}, {'':1}, {'FP':0}, {'FP':1,'fp':2}])
def test_bad_or_ambiguous_identity_binding_refused(aliases):
    with pytest.raises(ValueError): parse_fingerprint_evidence(payload(),filename='synthetic.xlsx',verified_aliases=aliases)


@pytest.mark.parametrize('cell,valid_count',[('zz',0),('08:00 xx 16:00',2),('25:00 08:00',1)])
def test_unreadable_remainder_keeps_valid_occurrences_and_day_provenance(cell,valid_count):
    raw=workbook_bytes([('Att.log report',[
        ['AC-No.','Name','Date','Check Time'],['SYN','Synthetic',date(2026,11,1),cell]])])
    result=parse_fingerprint_evidence(raw,filename='synthetic.xlsx',verified_aliases={'SYN':1})
    assert len(result.occurrences)==valid_count
    error=next(i for i in result.issues if i['reason']=='invalid_time_value')
    assert error['employee_id']==1 and error['work_date']==date(2026,11,1)
    assert error['sheet']=='Att.log report' and error['row']==2 and error['value']==cell


@pytest.mark.parametrize('value',[0.99999,0.9999])
def test_numeric_late_time_cannot_roll_to_same_day_midnight(value):
    raw=workbook_bytes([('Att.log report',[
        ['AC-No.','Name','Date','Check Time'],['SYN','Synthetic',date(2026,11,1),value]])])
    result=parse_fingerprint_evidence(raw,filename='synthetic.xlsx',verified_aliases={'SYN':1})
    assert result.occurrences[0]['punch_at'].hour==23


def test_evidence_occurrence_mapping_is_readonly():
    result=parse_fingerprint_evidence(payload(),filename='synthetic.xlsx',verified_aliases={'FP-1':1})
    with pytest.raises(TypeError): result.occurrences[0]['employee_id']=99


def test_invalid_date_keeps_identity_row_and_unreadable_time_evidence():
    raw=workbook_bytes([('Att.log report',[
        ['AC-No.','Name','Date','Check Time'],['SYN','Synthetic','bad-date','08:00 16:00']])])
    result=parse_fingerprint_evidence(raw,filename='synthetic.xlsx',verified_aliases={'SYN':1})
    assert not result.occurrences
    error=next(i for i in result.issues if i['reason']=='invalid_or_missing_date')
    assert error['employee_id']==1 and error['work_date'] is None
    assert error['raw_time_value']=='08:00 16:00' and error['row']==2


def test_partial_matrix_tokens_keep_date_and_duplicates():
    raw=workbook_bytes([('Att.log report',[
        ['Attendance Record Report'],['Att. Time','2026-11-01 ~ 2026-11-30'],list(range(1,31)),
        ['ID:','SYN','Name:','Synthetic'],['08:0008:00 xx 16:00']+[None]*29])])
    result=parse_fingerprint_evidence(raw,filename='synthetic.xlsx',verified_aliases={'SYN':1})
    assert len(result.occurrences)==3
    error=next(i for i in result.issues if i['reason']=='invalid_time_value')
    assert error['employee_id']==1 and error['work_date']==date(2026,11,1)
