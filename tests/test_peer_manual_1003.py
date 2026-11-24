"""Independent synthetic checks for package 1003. No production data or writes."""
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from hashlib import sha256
from io import BytesIO

import pytest
from openpyxl import load_workbook
from app.department_workbook import BALANCE_SHEET, ENTRY_SHEET, _decimal, build_department_workbook, inspect_department_return
from app.department_manifest import encode_manifest, decode_manifest
from app.department_workbook_source import WorkbookSourceFacts, read_workbook_source
from app.leave_manual_opening import (prepare_manual_opening, review_manual_opening, manual_opening_projection_inputs,
    manual_opening_projection_preview, build_manual_opening_preview_workbook)
from app.leave_policy_preview import LeavePolicy
from app.models import Employee, EmployeeServicePeriod
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context

@pytest.fixture
def manual(history_context):
    db, _, auth, fact = history_context
    employee = db.get(Employee, fact.employee_id)
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=date(2025,1,1)))
    db.flush()
    rows = [prepare_manual_opening(db, employee_id=employee.id, code=c, days=Decimal('8.25'),
        effective_start=date(2026,5,17), reason='Independent synthetic review', **auth) for c in ('A','H','W')]
    reviews = [review_manual_opening(r) for r in rows]
    facts = read_workbook_source(db, department_id=employee.department_id, employee_ids=(employee.id,),
        window_start=date(2026,1,1), month_start=date(2026,5,1), policy_digest='a'*64, expected_epoch=auth['expected_epoch'])
    args = dict(db=db, reviews=reviews, facts=facts,
        policy=LeavePolicy(False,False,True,False,'calendar','prorated',False,(90,180)),
        annual_tiers={(employee.id,date(2026,m,1)):15 for m in range(1,6)}, planning_target_month=True)
    return db, auth, employee, rows, reviews, facts, args

@pytest.mark.parametrize('field,value', [('proposal_id','f'*64),('proposal_sha256','0'*64),('days','9'),
    ('employee_id',2147483647),('code','W'),('effective_start','2026-05-01'),('reason','Changed'),
    ('requires_human_review',False),('applied',True)])
def test_review_is_bound_to_selected_stored_row(manual,field,value):
    *_, reviews, facts, args = manual
    changed = [dict(r) for r in reviews]
    changed[0][field] = value
    with pytest.raises(ValueError):
        manual_opening_projection_preview(**{**args,'reviews':changed})

@pytest.mark.parametrize('mode',['json','checksum','delete','other_id'])
def test_row_corruption_absence_and_wrong_selection_rejected(manual,mode):
    db,auth,employee,rows,reviews,facts,args = manual
    if mode == 'json': rows[0].proposal_json += ' '
    elif mode == 'checksum': rows[0].proposal_sha256 = '0'*64
    elif mode == 'delete': db.delete(rows[0])
    else: reviews[0] = {**reviews[0], 'proposal_id':rows[1].id}
    db.flush()
    with pytest.raises(ValueError): manual_opening_projection_preview(**args)

@pytest.mark.parametrize('days',['-1','NaN','sNaN','Infinity','-Infinity'])
def test_dict_sign_and_finiteness(manual,days):
    *_, reviews, facts, args = manual
    changed = [dict(r) for r in reviews]; changed[0]['days']=days
    with pytest.raises(ValueError): manual_opening_projection_inputs(reviews=changed,facts=facts)

@pytest.mark.parametrize('days',['-1','NaN','Infinity'])
def test_stored_manual_sign_and_finiteness_even_with_matching_checksum(manual,days):
    db,auth,employee,rows,reviews,facts,args = manual
    value=json.loads(rows[0].proposal_json);value['entries'][0]['days']=days
    rows[0].proposal_json=json.dumps(value);rows[0].proposal_sha256=sha256(rows[0].proposal_json.encode()).hexdigest()
    db.flush()
    with pytest.raises(ValueError): review_manual_opening(rows[0])

@pytest.mark.parametrize('spellings',[('1','1.00','1E+0'),('0','-0.00','0E-1000'),
    ('123.4500','123.45','1.2345E+2'),('0.0000001','1E-7','0.0000001000')])
def test_equal_value_spellings_dedupe(manual,spellings):
    db,auth,employee,*_=manual
    rows=[prepare_manual_opening(db,employee_id=employee.id,code='A',days=Decimal(s),
        effective_start=date(2026,5,1),reason='Canonical synthetic',**auth) for s in spellings]
    assert len({r.id for r in rows})==1
    assert Decimal(review_manual_opening(rows[0])['days'])==Decimal(spellings[0])

@pytest.mark.parametrize('days,accepted',[('9.999E+12',True),('1E+13',False),('1E-100',True),('1E-101',False),
    ('1.'+'2'*99,True),('1.'+'2'*100,False)])
def test_existing_numeric_envelope_used_for_manual_preparation(manual,days,accepted):
    db,auth,employee,*_=manual
    amount=Decimal(days)
    if accepted:
        _decimal(amount)
        row=prepare_manual_opening(db,employee_id=employee.id,code='A',days=amount,
            effective_start=date(2026,5,1),reason='Envelope synthetic',**auth)
        assert Decimal(review_manual_opening(row)['days'])==amount
    else:
        with pytest.raises(ValueError): _decimal(amount)
        with pytest.raises(ValueError):
            prepare_manual_opening(db,employee_id=employee.id,code='A',days=amount,
                effective_start=date(2026,5,1),reason='Envelope synthetic',**auth)

def test_empty_employee_scope_rejected():
    facts=WorkbookSourceFacts(json.dumps({'window_start':'2026-01-01','month_start':'2026-05-01',
        'employees':[],'service_periods':[]}), 'a'*64, (), ())
    with pytest.raises(ValueError,match='Nonempty'): manual_opening_projection_inputs(reviews=[],facts=facts)

@pytest.mark.parametrize('change',['basis','employees','source_digest','planning_flag','unresolved'])
def test_projection_change_detection(manual,change):
    *_,args=manual
    projection=manual_opening_projection_preview(**args)
    fields={'basis':dict(opening_effective_start=date(2026,5,1)),
        'employees':dict(employees=(replace(projection.employees[0],balance_usage_from=date(2026,5,1)),)),
        'source_digest':dict(source_digest='0'*64),'planning_flag':dict(establishes_spendable_credit=True),
        'unresolved':dict(unresolved_target_days=())}[change]
    with pytest.raises(ValueError,match='changed'):
        build_manual_opening_preview_workbook(projection=replace(projection,**fields),department='Synthetic',month_start=date(2026,5,1))

@pytest.mark.parametrize('builder',['plain','guarded'])
def test_midmonth_basis_formulas_manifest_and_full_attendance(manual,builder):
    *_,args=manual
    projection=manual_opening_projection_preview(**args)
    if builder=='plain':
        payload,manifest=build_department_workbook(department='Synthetic',month_start=date(2026,5,1),employees=projection.employees)
    else:
        payload,manifest=build_manual_opening_preview_workbook(projection=projection,department='Synthetic',month_start=date(2026,5,1))
    wb=load_workbook(BytesIO(payload))
    for cell,code in [('E7','A'),('I7','H'),('M7','W')]:
        assert 'S7:AG7' in wb[BALANCE_SHEET][cell].value
        assert '"'+code+'"' in wb[BALANCE_SHEET][cell].value
    assert '2026-05-17' in wb[BALANCE_SHEET]['A3'].value
    assert all(not wb[ENTRY_SHEET].cell(7,d+2).protection.locked for d in range(1,32))
    wb.close()
    assert manifest.employees[0].balance_usage_from==date(2026,5,17)
    restored=decode_manifest(encode_manifest(manifest))
    assert restored==manifest
    assert not inspect_department_return(payload,restored).issues

def test_plain_builder_cannot_override_employee_basis(manual):
    *_,args=manual
    projection=manual_opening_projection_preview(**args)
    with pytest.raises(ValueError,match='Conflicting'):
        build_department_workbook(department='Synthetic',month_start=date(2026,5,1),employees=projection.employees,
            balance_usage_from=date(2026,5,1))

def test_old_manifest_without_optional_basis_remains_readable():
    from tests.test_department_workbook import make_sample
    payload,manifest=make_sample()
    obj=json.loads(encode_manifest(manifest))
    for employee in obj['manifest']['employees']: employee.pop('balance_usage_from')
    restored=decode_manifest(json.dumps(obj))
    assert restored==manifest
    assert not inspect_department_return(payload,restored).issues

@pytest.mark.parametrize('field,value',[('source_sheet','Synthetic spreadsheet'),('kind','debt_carry')])
def test_stored_nonmanual_or_nonopening_type_is_refused(manual,field,value):
    db,auth,employee,rows,reviews,facts,args=manual
    value_json=json.loads(rows[0].proposal_json)
    value_json['entries'][0][field]=value
    rows[0].proposal_json=json.dumps(value_json)
    rows[0].proposal_sha256=sha256(rows[0].proposal_json.encode()).hexdigest()
    db.flush()
    with pytest.raises(ValueError,match='single manual'):
        review_manual_opening(rows[0])

@pytest.mark.parametrize('days,accepted',[('9.999E+12',True),('1E+13',False),('1E-100',True),('1E-101',False),
    ('1.'+'2'*99,True),('1.'+'2'*100,False)])
def test_dict_uses_existing_numeric_envelope(manual,days,accepted):
    *_,reviews,facts,args=manual
    changed=[{**r,'days':days} for r in reviews]
    if accepted:
        result,basis=manual_opening_projection_inputs(reviews=changed,facts=facts)
        assert set(result.values())=={Decimal(days)}
        assert basis==date(2026,5,17)
    else:
        with pytest.raises(ValueError): manual_opening_projection_inputs(reviews=changed,facts=facts)
