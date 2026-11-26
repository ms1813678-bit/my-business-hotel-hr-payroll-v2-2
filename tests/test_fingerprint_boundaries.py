"""Small deterministic raw-only boundary regressions, no stress payloads."""
from datetime import date, datetime, time
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.attendance_import import _evidence_time_tokens
from app.fingerprint_evidence import parse_fingerprint_evidence


def payload(rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def parse(raw, filename='synthetic.xlsx'):
    return parse_fingerprint_evidence(raw, filename=filename, verified_aliases={'SYN':1})


def test_all_exact_minute_fractions_and_excel_precision_keep_minute():
    for minute in range(1440):
        expected = time(minute//60, minute%60)
        for value in (minute/1440, float(format(minute/1440, '.15g'))):
            tokens, unreadable = _evidence_time_tokens(value)
            assert tokens == [expected]
            assert not unreadable
    tokens, unreadable = _evidence_time_tokens(.99999)
    assert tokens == [time(23,59,59)] and unreadable


def test_embedded_different_date_is_not_reassigned():
    result = parse(payload([['ID','Date','Time'],['SYN',date(2026,11,1),datetime(2026,11,5,8)]]))
    assert not result.occurrences
    issue = next(i for i in result.issues if i['reason']=='invalid_time_value')
    assert issue['employee_id']==1 and issue['work_date']==date(2026,11,1)
    assert '2026-11-05' in issue['value']


def test_sparse_shape_refused_before_row_iteration():
    book = Workbook()
    book.active['XFD2']='synthetic'
    stream=BytesIO();book.save(stream)
    with pytest.raises(ValueError,match='scan_limit'):
        parse(stream.getvalue())


def test_csv_small_configured_row_limit(monkeypatch):
    monkeypatch.setattr('app.attendance_import.RAW_MAX_ROWS', 2)
    with pytest.raises(ValueError,match='scan_limit'):
        parse(b'ID,Date,Time\nSYN,2026-11-01,08:00\nSYN,2026-11-02,08:00\n','synthetic.csv')


def test_small_event_and_issue_limits(monkeypatch):
    monkeypatch.setattr('app.attendance_import.RAW_MAX_EVENTS', 1)
    with pytest.raises(ValueError,match='scan_limit'):
        parse(payload([['ID','Date','Time'],['SYN',date(2026,11,1),'08:00 16:00']]))
    monkeypatch.setattr('app.attendance_import.RAW_MAX_ISSUES', 1)
    with pytest.raises(ValueError,match='scan_limit'):
        parse(payload([['ID','Date','Time'],['SYN',date(2026,11,1),'zz'],['SYN',date(2026,11,2),'zz']]))


def test_duplicate_issue_has_employee_day():
    result=parse(payload([['ID','Date','Time'],['SYN',date(2026,11,1),'08:00 08:00']]))
    issue=next(i for i in result.issues if i['reason']=='duplicate_punch')
    assert issue['employee_id']==1 and issue['work_date']==date(2026,11,1)


def test_missing_matrix_data_and_bad_csv_taint_review_not_clean_leave():
    from app.department_fingerprint_review import preview_department_fingerprint_review
    from app.department_workbook import WorkbookReturnPreview, ReturnedDayEvidence
    day=ReturnedDayEvidence(1,date(2026,11,1),'W','W',None,'S','C1')
    department=WorkbookReturnPreview('a'*64,(),(day,))
    missing=parse(payload([['Attendance Record Report'],['Att. Time','2026-11-01 ~ 2026-11-30'],
        list(range(1,31)),['ID:','SYN','Name:','Synthetic']]))
    assert any(i['reason']=='missing_employee_data_row' and i['employee_id']==1 for i in missing.issues)
    malformed=parse(b'ID,Date,Time\nSYN,2026-11-01,08:00,extra\n','synthetic.csv')
    issue=next(i for i in malformed.issues if i['reason']=='invalid_row_column_count')
    assert issue['employee_id']==1 and issue['work_date']==date(2026,11,1)
    for source in (missing,malformed):
        result=preview_department_fingerprint_review(department=department,fingerprint=source)
        assert result.days[0].review_state=='unreadable_fingerprint_evidence_needs_review'
        assert result.days[0].unreadable_source_evidence


@pytest.mark.parametrize('dimension',['A1:C2',None])
def test_xlsx_dimension_hints_cannot_hide_rows(dimension):
    import re
    from zipfile import ZipFile
    raw=payload([['ID','Date','Time']]+[['SYN',date(2026,11,day),'08:00'] for day in range(1,6)])
    output=BytesIO()
    with ZipFile(BytesIO(raw)) as original, ZipFile(output,'w') as changed:
        for member in original.infolist():
            value=original.read(member.filename)
            if member.filename=='xl/worksheets/sheet1.xml':
                value=re.sub(rb'<dimension[^>]*/>', b'' if dimension is None else ('<dimension ref="'+dimension+'"/>').encode(),value)
            changed.writestr(member,value)
    result=parse(output.getvalue())
    assert len(result.occurrences)==5
    assert result.occurrences[-1]['punch_at'].date()==date(2026,11,5)


def test_empty_matrix_id_does_not_consume_name_label():
    result=parse(payload([['Attendance Record Report'],['Att. Time','2026-11-01 ~ 2026-11-30'],
        list(range(1,31)),['ID:',None,'Name:','SYN'],['08:00']]))
    assert result.occurrences[0]['employee_id'] is None
    assert result.occurrences[0]['raw_employee_code'] is None


def test_zero_row_coordinate_refused():
    from zipfile import ZipFile
    raw=payload([['ID','Date','Time'],['SYN',date(2026,11,1),'08:00']])
    output=BytesIO()
    with ZipFile(BytesIO(raw)) as original,ZipFile(output,'w') as changed:
        for member in original.infolist():
            value=original.read(member.filename)
            if member.filename=='xl/worksheets/sheet1.xml': value=value.replace(b'r="C2"',b'r="C0"')
            changed.writestr(member,value)
    with pytest.raises(ValueError): parse(output.getvalue())


@pytest.mark.parametrize('identity',[float('nan'),float('inf'),True])
def test_invalid_numeric_matrix_identity_is_attributed_not_crash(monkeypatch,identity):
    rows=[['Attendance Record Report'],['Att. Time','2026-11-01 ~ 2026-11-30'],
        list(range(1,31)),['ID:',identity,'Name:','SYN'],['08:00']]
    monkeypatch.setattr('app.attendance_import._load_rows',lambda *a,**k:[('Synthetic',rows,0)])
    result=parse(b'synthetic',filename='synthetic.csv')
    assert any(i['reason']=='invalid_employee_identity' for i in result.issues)
    assert all(p['employee_id'] is None for p in result.occurrences)



def test_exact_joined_hhmm_is_readable_but_seconds_joins_stay_ambiguous():
    # Technical settlement relayed by Main 2026-11-26 (replaces the earlier "all joins ambiguous" pin): fixed-width
    # HH:MM joins split one way only; a join involving seconds is ambiguous and stays unreadable.
    tokens,unreadable=_evidence_time_tokens('08:0016:00')
    assert tokens==[time(8),time(16)] and not unreadable
    tokens,unreadable=_evidence_time_tokens('08:00:0016:00')
    assert tokens==[time(8,0,0),time(16)] and unreadable



@pytest.mark.parametrize('filename',['synthetic.xls','synthetic.xlsx'])
def test_invalid_binary_workbooks_refuse_controlled(filename):
    with pytest.raises(ValueError): parse(b'not a workbook',filename=filename)


@pytest.mark.parametrize('text',['03/04/2026','25/12/2026','12/25/2026'])
def test_localized_text_dates_are_unresolved_without_date_order_contract(text):
    result=parse(('ID,Date,Time\nSYN,'+text+',08:00\n').encode(),'synthetic.csv')
    assert not result.occurrences
    assert result.issues[0]['work_date'] is None and result.issues[0]['value']==text


def test_tabular_missing_code_never_links_name_equal_to_alias():
    result=parse(payload([['ID','Name','Date','Time'],[None,'SYN',date(2026,11,1),'08:00']]))
    assert result.occurrences[0]['employee_id'] is None


def test_corrupt_ole_and_incomplete_zip_are_controlled():
    from zipfile import ZipFile
    with pytest.raises(ValueError): parse(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'+b'\0'*600,'synthetic.xls')
    stream=BytesIO()
    with ZipFile(stream,'w') as z: z.writestr('x.txt','x')
    with pytest.raises(ValueError): parse(stream.getvalue())



def malformed_decoder_cases():
    from zipfile import ZipFile
    import struct
    original=payload([['ID','Date','Time'],['SYN',date(2026,11,1),'08:00']])
    with ZipFile(BytesIO(original)) as z: parts={n:z.read(n) for n in z.namelist()}
    cases=[]
    for name in ('xl/styles.xml','xl/_rels/workbook.xml.rels','[Content_Types].xml'):
        stream=BytesIO()
        with ZipFile(stream,'w') as z:
            for key,value in parts.items(): z.writestr(key,b'<a' if key==name else value)
        cases.append(('synthetic.xlsx',stream.getvalue()))
    stream=BytesIO()
    with ZipFile(stream,'w') as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr('xl/workbook.xml','<workbook/>')
    cases.append(('synthetic.xlsx',stream.getvalue()))
    cases.append(('synthetic.xls',b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'+b'\0'*16+struct.pack('<HHH',0x3e,3,0xfffe)+b'\x09\0'+b'\xff'*1000))
    return cases


@pytest.mark.parametrize('filename,source',malformed_decoder_cases())
def test_malformed_decoder_failures_controlled(filename,source):
    with pytest.raises(ValueError): parse(source,filename)
