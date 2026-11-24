"""Integrated synthetic pending preparation only, no accepted opening effect."""
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.department_preparation_workflow import (
    prepare_department_return_fingerprint_review,
    prepare_manual_department_export,
)
from app.department_workbook import ENTRY_SHEET
from app.fingerprint_evidence import parse_fingerprint_evidence
from app.leave_manual_opening_groups import persist_manual_opening_group
from app.leave_policy_preview import LeavePolicy
from app.models import Employee, EmployeeServicePeriod, LeaveBalanceTransaction, LeaveRegisterEntry
from tests.test_attendance_import import workbook_bytes
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401


def test_persisted_manual_groups_to_locked_planning_export_return_and_punch_review(history_context):  # noqa: F811
    db, _actor, auth, fact=history_context
    employee=db.get(Employee,fact.employee_id)
    db.add(EmployeeServicePeriod(employee_id=employee.id,start_date=date(2025,1,1))); db.flush()
    group,_=persist_manual_opening_group(db,employee_id=employee.id,
        balances={'A':Decimal('8.25'),'W':Decimal(2),'H':Decimal(0)},
        effective_start=date(2026,5,17),reason='Synthetic joined pending journey',**auth)
    months=tuple(date(2026,m,1) for m in range(1,6))
    args=dict(group_ids=(group.id,),department_id=employee.department_id,month_start=date(2026,5,1),
        policy=LeavePolicy(False,False,True,False,'calendar','prorated',False,(90,180)),
        current_insurance_years={(employee.id,m):None for m in months},age_evaluation_dates={m:m for m in months},**auth)
    before=[db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,LeaveRegisterEntry)]
    export=prepare_manual_department_export(db,**args)
    assert export.review_only and not export.establishes_spendable_credit
    wb=load_workbook(BytesIO(export.payload))
    assert all('قيد المراجعة' in sheet['A3'].value for sheet in wb)
    wb[ENTRY_SHEET]['S7']='P'; wb[ENTRY_SHEET]['T7']='A'
    out=BytesIO();wb.save(out);wb.close()
    fingerprint=parse_fingerprint_evidence(workbook_bytes([('Att.log report',[
        ['AC-No.','Name','Date','Check Time'],['SYN','Synthetic',date(2026,5,17),'08:00 08:00 16:00']
    ])]),filename='synthetic.xlsx',verified_aliases={'SYN':employee.id})
    receipt,preview=prepare_department_return_fingerprint_review(db,export_id=export.export_id,payload=out.getvalue(),
        fingerprint=fingerprint,**args)
    assert receipt>0
    assert len(preview.days[16].calendar_date_occurrences)==3
    assert preview.days[17].review_state=='explicit_leave_no_punch_required'
    assert not preview.establishes_attendance_or_balance
    assert [db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,LeaveRegisterEntry)]==before
    # A changed selected proposal changes bound source, making old return stale.
    changed,_=persist_manual_opening_group(db,employee_id=employee.id,
        balances={'A':Decimal('9.25'),'W':Decimal(2),'H':Decimal(0)},
        effective_start=date(2026,5,17),reason='Synthetic joined pending journey',**auth)
    refreshed=prepare_manual_department_export(db,**{**args,'group_ids':(changed.id,)})
    assert refreshed.source_digest!=export.source_digest
    with pytest.raises(ValueError,match='source changed'):
        prepare_department_return_fingerprint_review(db,export_id=export.export_id,payload=out.getvalue(),
            fingerprint=fingerprint,**{**args,'group_ids':(changed.id,)})

    # Policy and tier inputs are source, not merely display preferences.
    from dataclasses import replace
    with pytest.raises(ValueError,match='source changed'):
        prepare_department_return_fingerprint_review(db,export_id=export.export_id,payload=out.getvalue(),
            fingerprint=fingerprint,**{**args,'policy':replace(args['policy'],annual_deduct_sick=True)})
    # Changed tier schedule from a changed legitimate source fact invalidates.
    employee.hire_date=date(2026,1,1);db.flush()
    with pytest.raises(ValueError,match='source changed'):
        prepare_department_return_fingerprint_review(db,export_id=export.export_id,payload=out.getvalue(),
            fingerprint=fingerprint,**args)
    employee.hire_date=date(2025,1,1);db.flush()
    from app.models import Department
    wrong=Department(name='Synthetic wrong department',active=True);db.add(wrong);db.flush()
    with pytest.raises(ValueError,match='selection no longer matches'):
        prepare_department_return_fingerprint_review(db,export_id=export.export_id,payload=out.getvalue(),
            fingerprint=fingerprint,**{**args,'department_id':wrong.id})
    with pytest.raises(ValueError):
        prepare_department_return_fingerprint_review(db,export_id=export.export_id,payload=out.getvalue(),
            fingerprint=fingerprint,**{**args,'month_start':date(2026,4,1)})
    corrupted=load_workbook(BytesIO(out.getvalue()));corrupted[ENTRY_SHEET]['A3']='tampered'
    bad=BytesIO();corrupted.save(bad);corrupted.close()
    with pytest.raises(ValueError,match='unresolved'):
        prepare_department_return_fingerprint_review(db,export_id=export.export_id,payload=bad.getvalue(),
            fingerprint=fingerprint,**args)
    # Actual live source changed but same group selection: stale bytes fail.
    from app.models import LeaveHoliday
    db.add(LeaveHoliday(holiday_date=date(2026,5,19),name='Synthetic new holiday',created_by='synthetic'))
    db.flush()
    with pytest.raises(ValueError,match='source changed'):
        prepare_department_return_fingerprint_review(db,export_id=export.export_id,payload=out.getvalue(),
            fingerprint=fingerprint,**args)


def test_opening_before_target_month_counts_postbasis_prior_usage_once(history_context):  # noqa: F811
    db,_actor,auth,fact=history_context
    employee=db.get(Employee,fact.employee_id)
    db.add(EmployeeServicePeriod(employee_id=employee.id,start_date=date(2025,1,1)))
    for day in range(17,32):
        db.add(LeaveRegisterEntry(employee_id=employee.id,work_date=date(2026,5,day),
            code='A' if day==20 else '',source='synthetic',recorded_by='synthetic'))
    db.flush()
    group,_=persist_manual_opening_group(db,employee_id=employee.id,
        balances={'A':Decimal('8.25'),'W':Decimal(2),'H':Decimal(0)},
        effective_start=date(2026,5,17),reason='Synthetic earlier basis',**auth)
    months=tuple(date(2026,m,1) for m in range(1,7))
    export=prepare_manual_department_export(db,group_ids=(group.id,),department_id=employee.department_id,
        month_start=date(2026,6,1),policy=LeavePolicy(False,False,True,False,'calendar','prorated',False,(90,180)),
        current_insurance_years={(employee.id,m):None for m in months},age_evaluation_dates={m:m for m in months},**auth)
    from app.department_manifest import decode_manifest
    from app.models import DepartmentWorkbookExport
    manifest=decode_manifest(db.get(DepartmentWorkbookExport,export.export_id).manifest_json)
    assert manifest.employees[0].annual.carry==Decimal('8.25')
    assert manifest.employees[0].annual.used_before_month==Decimal(1)
    assert manifest.employees[0].balance_usage_from==date(2026,6,1)
    workbook=load_workbook(BytesIO(export.payload))
    from app.department_workbook import BALANCE_SHEET
    assert 'C7:AF7' in workbook[BALANCE_SHEET]['E7'].value
    workbook.close()


def test_confirmed_selection_consumed_without_policy_or_opening_defaults(history_context):
    from app.department_preparation_workflow import prepare_confirmed_source_department_export
    from app.leave_opening_source import confirm_opening_source,review_opening_source
    from app.leave_policy_store import persist_policy_version,read_policy_version
    db,_actor,auth,fact=history_context
    employee=db.get(Employee,fact.employee_id)
    db.add(EmployeeServicePeriod(employee_id=employee.id,start_date=date(2025,1,1)));db.flush()
    group,_=persist_manual_opening_group(db,employee_id=employee.id,
        balances={'A':Decimal(8),'W':Decimal(2),'H':Decimal(0)},
        effective_start=date(2026,5,17),reason='Synthetic source',**auth)
    policy=persist_policy_version(db,policy=LeavePolicy(False,False,True,False,'calendar','prorated',False,(90,180)),
        reason='Synthetic explicit policy',**auth)
    review=review_opening_source(db,group_id=group.id,policy_id=policy.id,**auth)
    selected=confirm_opening_source(db,group_id=group.id,policy_id=policy.id,
        expected_previous_id=None,reviewed_source_sha256=review['source_sha256'],
        reason='Synthetic source confirmation',human_confirmed=True,**auth)
    months=tuple(date(2026,m,1) for m in range(1,6))
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    result=prepare_confirmed_source_department_export(db,selection_ids=(selected.id,),
        department_id=employee.department_id,month_start=date(2026,5,1),
        current_insurance_years={(employee.id,m):None for m in months},
        age_evaluation_dates={m:m for m in months},**auth)
    assert result.review_only and not result.establishes_spendable_credit
    from app.department_contract import export_confirmed_department,review_confirmed_department_return
    contract_export=export_confirmed_department(db,selection_ids=(selected.id,),department_id=employee.department_id,
        month_start=date(2026,5,1),current_insurance_years={(employee.id,m):None for m in months},
        age_evaluation_dates={m:m for m in months},**auth)
    args=dict(selection_ids=(selected.id,),department_id=employee.department_id,month_start=date(2026,5,1),
        current_insurance_years={(employee.id,m):None for m in months},age_evaluation_dates={m:m for m in months},**auth)
    from app.models import Department
    wrong=Department(name='Synthetic nonmember',active=True);db.add(wrong);db.flush()
    for altered in (
        {'department_id':wrong.id},
        {'month_start':date(2026,5,2)},
        {'age_evaluation_dates':{**args['age_evaluation_dates'],date(2026,5,1):date(2026,4,30)}},
        {'current_insurance_years':{**args['current_insurance_years'],(employee.id+999,date(2026,5,1)):None}},
        {'expected_epoch':'synthetic-stale-epoch'},
        {'session_version':auth['session_version']+1},
    ):
        with pytest.raises(ValueError): export_confirmed_department(db,**{**args,**altered})
    receipt=review_confirmed_department_return(db,export_id=contract_export.export_id,payload=contract_export.payload,**auth)
    assert receipt.id>0
    from app.department_fingerprint_workflow import compare_department_fingerprint
    source=workbook_bytes([('Synthetic fingerprint',[
        ['ID','Date','Time'],[employee.code,date(2026,5,17),'08:00 08:00 bad 16:00']])])
    compared=compare_department_fingerprint(db,export_id=contract_export.export_id,
        department_bytes=contract_export.payload,fingerprint_bytes=source,fingerprint_filename='synthetic.xlsx',**auth)
    assert compared.fingerprint_bytes==source and 'invalid_time_value' in compared.review_json
    import json
    from hashlib import sha256
    evidence=json.loads(compared.review_json)
    assert [employee.code,employee.code.casefold(),employee.id] in evidence['alias_bindings']
    assert evidence['alias_bindings_sha256']==sha256(json.dumps(evidence['alias_bindings'],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    with db.begin_nested() as collision_tx:
        outsider=Employee(code='SYN-OUTSIDE',fingerprint_id=employee.code.lower(),full_name='Synthetic outside',
            department_id=wrong.id,position='Synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8))
        db.add(outsider);db.flush()
        with pytest.raises(ValueError,match='collision'):
            compare_department_fingerprint(db,export_id=contract_export.export_id,department_bytes=contract_export.payload,
                fingerprint_bytes=source,fingerprint_filename='synthetic.xlsx',**auth)
        collision_tx.rollback()

    from app.models import DepartmentFingerprintPreparation,DepartmentWorkbookPreparation
    comparison_count=db.scalar(select(func.count()).select_from(DepartmentFingerprintPreparation))
    receipt_count=db.scalar(select(func.count()).select_from(DepartmentWorkbookPreparation))
    with pytest.raises(ValueError):
        compare_department_fingerprint(db,export_id=contract_export.export_id,
            department_bytes=contract_export.payload,fingerprint_bytes=b'invalid',fingerprint_filename='synthetic.xlsx',**auth)
    assert db.scalar(select(func.count()).select_from(DepartmentFingerprintPreparation))==comparison_count
    assert db.scalar(select(func.count()).select_from(DepartmentWorkbookPreparation))==receipt_count


    pending=prepare_manual_department_export(db,group_ids=(group.id,),department_id=employee.department_id,
        month_start=date(2026,5,1),policy=read_policy_version(db,policy_id=policy.id,**auth),
        current_insurance_years={(employee.id,m):None for m in months},age_evaluation_dates={m:m for m in months},**auth)
    assert pending.source_digest!=result.source_digest
    from app.models import DepartmentPreparationContract
    contract=db.get(DepartmentPreparationContract,contract_export.export_id)
    original=contract.selection_json
    contract.selection_json=original+' ';db.flush()
    with pytest.raises(ValueError,match='contract'):
        review_confirmed_department_return(db,export_id=contract_export.export_id,payload=contract_export.payload,**auth)
    contract.selection_json=original;db.flush()
    from app.models import LeaveHoliday
    db.add(LeaveHoliday(holiday_date=date(2026,5,19),name='Synthetic changed source',created_by='synthetic'));db.flush()
    with pytest.raises(ValueError,match='source changed'):
        review_confirmed_department_return(db,export_id=contract_export.export_id,payload=contract_export.payload,**auth)

    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before


def test_actual_generated_chain_to_pending_attendance_registration(history_context):
    from app.department_preparation_workflow import prepare_confirmed_source_department_export
    from app.leave_opening_source import confirm_opening_source,review_opening_source
    from app.leave_policy_store import persist_policy_version,read_policy_version
    db,_actor,auth,fact=history_context
    employee=db.get(Employee,fact.employee_id)
    db.add(EmployeeServicePeriod(employee_id=employee.id,start_date=date(2025,1,1)));db.flush()
    group,_=persist_manual_opening_group(db,employee_id=employee.id,
        balances={'A':Decimal(8),'W':Decimal(2),'H':Decimal(0)},
        effective_start=date(2026,5,17),reason='Synthetic source',**auth)
    policy=persist_policy_version(db,policy=LeavePolicy(False,False,True,False,'calendar','prorated',False,(90,180)),
        reason='Synthetic explicit policy',**auth)
    review=review_opening_source(db,group_id=group.id,policy_id=policy.id,**auth)
    selected=confirm_opening_source(db,group_id=group.id,policy_id=policy.id,
        expected_previous_id=None,reviewed_source_sha256=review['source_sha256'],
        reason='Synthetic source confirmation',human_confirmed=True,**auth)
    months=tuple(date(2026,m,1) for m in range(1,6))
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    result=prepare_confirmed_source_department_export(db,selection_ids=(selected.id,),
        department_id=employee.department_id,month_start=date(2026,5,1),
        current_insurance_years={(employee.id,m):None for m in months},
        age_evaluation_dates={m:m for m in months},**auth)
    assert result.review_only and not result.establishes_spendable_credit
    from app.department_contract import export_confirmed_department,review_confirmed_department_return
    contract_export=export_confirmed_department(db,selection_ids=(selected.id,),department_id=employee.department_id,
        month_start=date(2026,5,1),current_insurance_years={(employee.id,m):None for m in months},
        age_evaluation_dates={m:m for m in months},**auth)
    from datetime import time
    from app.models import ShiftPattern,AttendanceDailyRecord,AttendanceEvidenceConsumption
    from app.department_fingerprint_workflow import compare_department_fingerprint
    from app.attendance_evidence_review import prepare_evidence_review,consume_evidence_review
    workbook=load_workbook(BytesIO(contract_export.payload));workbook[ENTRY_SHEET]['S7']='P'
    returned=BytesIO();workbook.save(returned);workbook.close()
    raw=b'ID,Date,Time\nSYN-HISTORY,2026-05-17,08:00 08:00 16:00\n'
    compared=compare_department_fingerprint(db,export_id=contract_export.export_id,department_bytes=returned.getvalue(),fingerprint_bytes=raw,fingerprint_filename='synthetic.csv',**auth)
    pattern=ShiftPattern(department_id=employee.department_id,code='SYN-CHAIN',name='Synthetic chain',start_time=time(8),end_time=time(16),match_before_minutes=0,match_after_minutes=0,active=True)
    db.add(pattern);db.flush()
    from app.models import AttendanceMonth
    db.add(AttendanceMonth(period_start=date(2026,5,1),period_end=date(2026,5,31),status='under_review',created_by=_actor.username));db.flush()
    row=prepare_evidence_review(db,operation_id='1'*64,comparison_id=compared.id,employee_id=employee.id,work_date=date(2026,5,17),windows=((date(2026,5,17),pattern.id),),selected_window=f'2026-05-17:{pattern.id}',in_ordinal=0,out_ordinal=2,reason='Synthetic actual generated workbook raw endpoints',**auth)
    record=consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
    assert record.status=='needs_review' and record.worked_minutes==480
    assert db.scalar(select(func.count()).select_from(AttendanceEvidenceConsumption))==1
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before
