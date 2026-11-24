from datetime import date,timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
import pytest
from app.department_workbook import (BalanceSnapshot,DepartmentWorkbookEmployee,
    build_department_workbook,inspect_department_return,BALANCE_SHEET,ENTRY_SHEET)


def make_sample(month=date(2026,5,1),count=2):
    from calendar import monthrange
    days=monthrange(month.year,month.month)[1]
    snapshot=BalanceSnapshot(Decimal('1.50'),Decimal('8.25'),Decimal('2.00'))
    employees=tuple(DepartmentWorkbookEmployee(i+1,str(1001+i),f'موظف تجريبي {i+1}',date(month.year,1,1),
        frozenset(month+timedelta(days=n) for n in range(days)),snapshot,snapshot,snapshot) for i in range(count))
    return build_department_workbook(department='قسم تجريبي',month_start=month,employees=employees,
        approved_print_logo=Path(__file__).resolve().parents[1]/'app/static/hotel-logo-print-approved.png')


def changed(payload,fn):
    wb=load_workbook(BytesIO(payload));fn(wb);out=BytesIO();wb.save(out);wb.close();return out.getvalue()


def test_exact_two_tabs_target_month_and_blank_evidence():
    payload,manifest=make_sample(date(2028,2,1))
    wb=load_workbook(BytesIO(payload));assert wb.sheetnames==[BALANCE_SHEET,ENTRY_SHEET]
    assert wb[ENTRY_SHEET].cell(6,31).value==29
    assert wb[ENTRY_SHEET].cell(6,32).value is None
    assert len(wb[ENTRY_SHEET]._images)==len(wb[BALANCE_SHEET]._images)==1
    assert not wb[ENTRY_SHEET].conditional_formatting
    result=inspect_department_return(payload,manifest)
    assert not result.issues and len(result.days)==58
    assert all(d.code is None and d.issue is None for d in result.days)


def test_last_employee_live_formula_and_editable_cells():
    payload,_=make_sample();wb=load_workbook(BytesIO(payload))
    assert 'C8:AG8' in wb[BALANCE_SHEET]['E8'].value
    assert '"A"' in wb[BALANCE_SHEET]['E8'].value
    assert not wb[ENTRY_SHEET]['C7'].protection.locked
    assert wb[BALANCE_SHEET]['C7'].protection.locked
    assert wb[ENTRY_SHEET]['B7'].protection.locked
    assert wb[BALANCE_SHEET]['F7'].value=='=C7+D7-E7'


def test_explicit_p_is_not_blank_and_lowercase_is_preserved():
    payload,manifest=make_sample()
    payload=changed(payload,lambda w:setattr(w[ENTRY_SHEET]['C7'],'value','p'))
    result=inspect_department_return(payload,manifest)
    assert not result.issues
    assert result.days[0].code=='P' and result.days[0].raw_value=='p'
    assert result.days[1].code is None


def test_protected_identity_change_refuses_clean_return():
    payload,manifest=make_sample()
    payload=changed(payload,lambda w:setattr(w[ENTRY_SHEET]['A7'],'value','9999'))
    assert inspect_department_return(payload,manifest).issues==('protected_content_changed',)


def test_formula_day_is_evidence_error_not_execution():
    payload,manifest=make_sample()
    payload=changed(payload,lambda w:setattr(w[ENTRY_SHEET]['C7'],'value','=1+1'))
    result=inspect_department_return(payload,manifest)
    assert result.days[0].code is None
    assert result.days[0].issue=='formula_is_not_day_evidence'


def test_bad_code_is_not_guessed_present_or_absent():
    payload,manifest=make_sample()
    payload=changed(payload,lambda w:setattr(w[ENTRY_SHEET]['C7'],'value','-'))
    assert inspect_department_return(payload,manifest).days[0].issue=='invalid_day_code'


def test_wrong_sheet_rejected_and_original_unchanged():
    payload,manifest=make_sample()
    with pytest.raises(ValueError):
        inspect_department_return(changed(payload,lambda w:w.create_sheet('extra')),manifest)
    assert not inspect_department_return(payload,manifest).issues


def test_names_are_text_not_formula():
    from dataclasses import replace
    payload,manifest=make_sample(count=1)
    employee=replace(manifest.employees[0],name='=1+1',code='@CODE')
    data,_=build_department_workbook(department='=EXAMPLE',month_start=manifest.month_start,employees=(employee,))
    wb=load_workbook(BytesIO(data));assert wb[ENTRY_SHEET]['B7'].data_type=='s'
    assert wb[ENTRY_SHEET]['B7'].value=='=1+1'


def test_employment_cells_outside_scope_remain_locked_and_detect_edits():
    from dataclasses import replace
    data,old=make_sample(count=1)
    employee=replace(old.employees[0],employed_dates=frozenset({date(2026,5,2)}))
    data,manifest=build_department_workbook(department='قسم',month_start=date(2026,5,1),employees=(employee,))
    wb=load_workbook(BytesIO(data));assert wb[ENTRY_SHEET]['C7'].protection.locked
    assert not wb[ENTRY_SHEET]['D7'].protection.locked
    result=inspect_department_return(data,manifest);assert len(result.days)==1
    tampered=changed(data,lambda w:setattr(w[ENTRY_SHEET]['C7'],'value','P'))
    assert inspect_department_return(tampered,manifest).issues==('protected_content_changed',)


def test_duplicate_identity_and_negative_snapshot_rejected():
    from dataclasses import replace
    _,old=make_sample(count=1);employee=old.employees[0]
    with pytest.raises(ValueError):
        build_department_workbook(department='قسم',month_start=date(2026,5,1),employees=(employee,employee))
    bad=replace(employee,annual=BalanceSnapshot(Decimal(0),Decimal(-1),Decimal(0)))
    with pytest.raises(ValueError):
        build_department_workbook(department='قسم',month_start=date(2026,5,1),employees=(bad,))


def test_altered_balance_formula_is_not_trusted():
    data,manifest=make_sample()
    tampered=changed(data,lambda w:setattr(w[BALANCE_SHEET]['F7'],'value','=9999'))
    assert inspect_department_return(tampered,manifest).issues==('protected_content_changed',)


def test_external_links_and_macro_entries_are_rejected():
    from zipfile import ZipFile,ZIP_DEFLATED
    data,manifest=make_sample()
    for entry in ['xl/externalLinks/externalLink1.xml','xl/vbaProject.bin']:
        out=BytesIO(data)
        with ZipFile(out,'a',ZIP_DEFLATED) as archive:archive.writestr(entry,b'synthetic')
        with pytest.raises(ValueError):inspect_department_return(out.getvalue(),manifest)


def test_ambiguous_zip_and_duplicate_xml_cells_refused_before_parser():
    from zipfile import ZipFile,ZIP_DEFLATED
    import xml.etree.ElementTree as ET
    import warnings
    data,manifest=make_sample()
    def repack(duplicate=False):
        out=BytesIO()
        with ZipFile(BytesIO(data)) as src,ZipFile(out,'w',ZIP_DEFLATED) as dst:
            for info in src.infolist():
                raw=src.read(info.filename)
                if info.filename=='xl/worksheets/sheet2.xml' and not duplicate:
                    root=ET.fromstring(raw); ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
                    row=root.find('{'+ns+'}sheetData/{'+ns+'}row[@r="7"]')
                    for code in ('A','P'):
                        c=ET.SubElement(row,'{'+ns+'}c',{'r':'C7','t':'inlineStr'})
                        ET.SubElement(ET.SubElement(c,'{'+ns+'}is'),'{'+ns+'}t').text=code
                    raw=ET.tostring(root)
                dst.writestr(info.filename,raw)
            if duplicate:dst.writestr('xl/worksheets/sheet2.xml',src.read('xl/worksheets/sheet2.xml'))
        return out.getvalue()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        for payload in (repack(),repack(True)):
            with pytest.raises(ValueError,match='Ambiguous'):inspect_department_return(payload,manifest)


def test_unexpected_defined_names_and_hyperlinks_refused():
    from openpyxl.workbook.defined_name import DefinedName
    data,manifest=make_sample()
    def name(w):w.defined_names.add(DefinedName('SyntheticName',attr_text='1+1'))
    for fn in (name,lambda w:setattr(w[ENTRY_SHEET]['A7'],'hyperlink','https://example.invalid/')):
        with pytest.raises(ValueError):inspect_department_return(changed(data,fn),manifest)


def test_workflow_shape_changes_are_not_clean_evidence():
    data,manifest=make_sample()
    for fn in (lambda w:setattr(w[ENTRY_SHEET].row_dimensions[7],'hidden',True),
               lambda w:w[ENTRY_SHEET].merge_cells('C7:D7'),
               lambda w:setattr(w[ENTRY_SHEET].protection,'sheet',False),
               lambda w:setattr(w[ENTRY_SHEET].data_validations,'dataValidation',[])):
        assert 'workbook_structure_changed' in inspect_department_return(changed(data,fn),manifest).issues


def test_manifest_identity_uniqueness_checked_on_import():
    from dataclasses import replace
    data,manifest=make_sample()
    duplicate=replace(manifest,employees=(manifest.employees[0],replace(manifest.employees[1],employee_id=1)))
    with pytest.raises(ValueError,match='Duplicate'):inspect_department_return(data,duplicate)


def test_unbounded_decimal_snapshots_fail_without_financial_rounding():
    from dataclasses import replace
    _,manifest=make_sample(count=1);employee=manifest.employees[0]
    for value in ('1E+400','1E-400'):
        with pytest.raises(ValueError,match='magnitude'):
            build_department_workbook(department='Synthetic',month_start=manifest.month_start,
                employees=(replace(employee,annual=replace(employee.annual,carry=Decimal(value))),))


def test_planning_projection_preserves_exact_authoritative_snapshot():
    from dataclasses import replace
    from app.leave_day_calculations import weekly_accrued
    _,manifest=make_sample(count=1);employee=manifest.employees[0]
    exact=weekly_accrued(31,0,0)
    employee=replace(employee,weekly=replace(employee.weekly,entitled_through_month_end=exact))
    payload,manifest=build_department_workbook(department='Synthetic',month_start=manifest.month_start,employees=(employee,))
    assert manifest.employees[0].weekly.entitled_through_month_end==exact
    wb=load_workbook(BytesIO(payload));assert str(wb[BALANCE_SHEET]['L7'].value)=='4.42857142857143'
    assert wb[BALANCE_SHEET]['L7'].number_format=='0.00'
    assert 'تقريبي' in wb[BALANCE_SHEET]['A3'].value
    assert inspect_department_return(payload,manifest).issues==()
    result=inspect_department_return(changed(payload,lambda w:setattr(w[ENTRY_SHEET]['C7'],'value','W')),manifest)
    assert result.days[0].code=='W' and not result.issues
    assert manifest.employees[0].weekly.entitled_through_month_end==exact
