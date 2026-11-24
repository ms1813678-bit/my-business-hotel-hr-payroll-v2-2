"""Synthetic builder-only checks for package 1023 pending label (no repo, no DB)."""
from datetime import date
from decimal import Decimal
from io import BytesIO
import pytest
from openpyxl import load_workbook
from app.department_workbook import *
from app.department_workbook import _structure_digest

def emp(i=1,code='E1'):
    s=lambda c,e,u: BalanceSnapshot(Decimal(c),Decimal(e),Decimal(u))
    dates=frozenset(date(2026,5,d) for d in range(1,32))
    return DepartmentWorkbookEmployee(i,code,'Synthetic '+code,date(2026,1,1),dates,s('8.25','10','0'),s(0,1,0),s(2,4,0))

def build(pending=True, usage=date(2026,5,17)):
    return build_department_workbook(department='Synthetic Dept',month_start=date(2026,5,1),employees=(emp(),emp(2,'E2')),balance_usage_from=usage,pending_manual_source=pending)

def test_both_sheets_labeled_pending():
    p,m=build()
    wb=load_workbook(BytesIO(p))
    for ws in wb:
        t=ws['A3'].value
        assert 'قيد المراجعة' in t and 'ليست رصيدًا معتمدًا' in t and 'ليست' in t and 'إذنًا بالصرف' in t
        assert 'الرصيد المعتمد' not in t   # old approved-system-balance notice gone
        assert '2026-05-17' in t
    # other approval-sounding text remaining on sheets?
    for ws in wb:
        for row in ws:
            for c in row:
                if isinstance(c.value,str) and 'المعتمد' in c.value: print('APPROVAL-WORD', ws.title, c.coordinate, c.value)

def test_default_unchanged_notice():
    p,_=build(False,None)
    wb=load_workbook(BytesIO(p))
    assert 'الرصيد المعتمد' in wb[BALANCE_SHEET]['A3'].value

def test_label_tamper_detected():
    p,m=build()
    for sheet in (BALANCE_SHEET,ENTRY_SHEET):
        wb=load_workbook(BytesIO(p)); wb[sheet]['A3']='x'
        o=BytesIO(); wb.save(o)
        r=inspect_department_return(o.getvalue(),m)
        assert 'protected_content_changed' in r.issues

def test_pending_marker_not_in_manifest_and_flag_strictly_bool():
    p,m=build()
    assert not hasattr(m,'pending_manual_source')
    with pytest.raises(ValueError):
        build_department_workbook(department='d',month_start=date(2026,5,1),employees=(emp(),),pending_manual_source=1)

def test_same_content_pending_vs_not_have_different_digest():
    _,a=build(True); _,b=build(False)
    assert a.immutable_digest!=b.immutable_digest
