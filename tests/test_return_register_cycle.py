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



def setup_return_chain(history_context,*,returned_code='P',punches=True,multi=False,second_code='A',consume_attendance=True):
    from app.department_preparation_workflow import prepare_confirmed_source_department_export
    from app.leave_opening_source import confirm_opening_source,review_opening_source
    from app.leave_policy_store import persist_policy_version,read_policy_version
    db,_actor,auth,fact=history_context
    employee=db.get(Employee,fact.employee_id)
    employee.hire_date=date(2026,5,30) if multi else date(2026,5,31)
    db.add(EmployeeServicePeriod(employee_id=employee.id,start_date=employee.hire_date));db.flush()
    group,_=persist_manual_opening_group(db,employee_id=employee.id,
        balances={'A':Decimal(8),'W':Decimal(2),'H':Decimal(0)},
        effective_start=employee.hire_date,reason='Synthetic source',**auth)
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
    workbook=load_workbook(BytesIO(contract_export.payload));workbook[ENTRY_SHEET]['AF7' if multi else 'AG7']=returned_code
    if multi:workbook[ENTRY_SHEET]['AG7']=second_code
    returned=BytesIO();workbook.save(returned);workbook.close()
    raw=b'ID,Date,Time\nSYN-HISTORY,2026-05-31,08:00 08:00 16:00\n' if punches else b'ID,Date,Time\nSYN-HISTORY,2026-06-01,08:00 16:00\n'
    if multi:raw=raw.replace(b'2026-05-31',b'2026-05-30')
    compared=compare_department_fingerprint(db,export_id=contract_export.export_id,department_bytes=returned.getvalue(),fingerprint_bytes=raw,fingerprint_filename='synthetic.csv',**auth)
    pattern=ShiftPattern(department_id=employee.department_id,code='SYN-CHAIN',name='Synthetic chain',start_time=time(8),end_time=time(16),match_before_minutes=0,match_after_minutes=0,active=True)
    db.add(pattern);db.flush()
    from app.models import AttendanceMonth
    db.add(AttendanceMonth(period_start=date(2026,5,1),period_end=date(2026,5,31),status='under_review',created_by=_actor.username));db.flush()
    if returned_code!='P' or not consume_attendance:return locals()
    row=prepare_evidence_review(db,operation_id='1'*64,comparison_id=compared.id,employee_id=employee.id,work_date=employee.hire_date,windows=((employee.hire_date,pattern.id),),selected_window=f'{employee.hire_date.isoformat()}:{pattern.id}',in_ordinal=0,out_ordinal=2,reason='Synthetic actual generated workbook raw endpoints',**auth)
    record=consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
    assert record.status=='needs_review' and record.worked_minutes==480
    assert db.scalar(select(func.count()).select_from(AttendanceEvidenceConsumption))==1
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before
    return locals()


@pytest.mark.skipif(__import__('os').environ.get('MANUAL_RETURN_REGISTER_CYCLE')!='1',reason='Dedicated synthetic_cycle DB, real compatible clients')
def test_actual_return_attendance_register_close_next_export(history_context,monkeypatch,tmp_path):
    from app.config import settings
    from app import backup_workflow as b
    from app.backup_crypto import set_passphrase
    from app.leave_synthetic_register import preview_synthetic_register,consume_synthetic_register
    from app.leave_payroll_read import get_month_summary
    from app.leave_synthetic_close import close_synthetic_month
    from app.models import SystemSetting,AuditLog,LeaveSyntheticRegisterConsumption,PayrollItem
    from app.department_preparation_workflow import prepare_confirmed_source_department_export
    from sqlalchemy import event
    ctx=setup_return_chain(history_context,multi=True)
    db=ctx['db'];employee=ctx['employee'];auth=ctx['auth'];comparison=ctx['compared']
    monkeypatch.setattr(settings,'leave_synthetic_register_enabled',True)
    monkeypatch.setattr(settings,'leave_synthetic_month_close_enabled',True)
    monkeypatch.setattr(settings,'app_env','test')
    root=tmp_path/'backups';root.mkdir();photos=tmp_path/'photos';photos.mkdir()
    validate=b.validate_backup_root
    monkeypatch.setattr(b,'validate_backup_root',lambda p,require_external=True:validate(p,require_external=False))
    monkeypatch.setattr(b,'PHOTO_DIRECTORY',photos)
    db.add(SystemSetting(key='backup_root',value=str(root),updated_by='synthetic'))
    set_passphrase(db,'synthetic-only-return-cycle','synthetic');db.flush();db.commit()
    selection=dict(selection_ids=(ctx['selected'].id,),department_id=employee.department_id,month_start=date(2026,5,1),
        current_insurance_years={(employee.id,date(2026,m,1)):None for m in range(1,6)},
        age_evaluation_dates={date(2026,m,1):date(2026,m,1) for m in range(1,6)},release_evaluated_on=date(2026,5,31))
    with pytest.raises(ValueError):get_month_summary(db,employee_id=employee.id,year=2026,month=5,**selection,**auth)
    before=[db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,PayrollItem)]
    proposals,digest=preview_synthetic_register(db,comparison_id=comparison.id,employee_id=employee.id,**auth)
    assert len(proposals)==2 and proposals[0]['code']=='' and proposals[1]['code']=='A'
    call=dict(operation_id='8'*64,comparison_id=comparison.id,employee_id=employee.id,reviewed_digest=digest,
              human_confirmed=True,reason='Synthetic explicit return acceptance',**auth)
    # Actual failure after entry flush cannot leave partial registration/link.
    def fail(session,ctx,objects):
        if any(isinstance(o,AuditLog) and o.action=='synthetic_return_registered' for o in session.new):raise RuntimeError('synthetic register audit failure')
    event.listen(db,'before_flush',fail)
    try:
        with pytest.raises(RuntimeError):consume_synthetic_register(db,**call)
    finally:event.remove(db,'before_flush',fail)
    assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==0
    accepted=consume_synthetic_register(db,**call)
    assert consume_synthetic_register(db,**call).id==accepted.id
    with pytest.raises(ValueError,match='already consumed'):consume_synthetic_register(db,**{**call,'operation_id':'9'*64})
    db.commit()
    summary=get_month_summary(db,employee_id=employee.id,year=2026,month=5,**selection,**auth)
    assert summary.present_days==1 and summary.employed_days==2
    closed=close_synthetic_month(db,year=2026,month=5,employee_selections={employee.id:selection},
        reviewed_summary_digests={employee.id:summary.source_digest},reason='Synthetic full return cycle',human_confirmed=True,**auth)
    db.commit()
    next_selection={**selection,'month_start':date(2026,6,1),
        'current_insurance_years':{(employee.id,date(2026,m,1)):None for m in range(1,7)},
        'age_evaluation_dates':{date(2026,m,1):date(2026,m,1) for m in range(1,7)}}
    next_selection.pop('release_evaluated_on')
    next_export=prepare_confirmed_source_department_export(db,**next_selection,**auth)
    assert next_export.review_only and not next_export.establishes_spendable_credit
    assert [db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,PayrollItem)]==before
    # The close's actual archive was taken AFTER register consumption and BEFORE
    # closure. Restore it, proving new nonempty consumption history survives.
    from app.models import BackupRun,LeaveMonthClose
    archive=db.get(BackupRun,closed.backup_run_id)
    archive_path=archive.archive_path;archive_sha=archive.sha256
    accepted_id=accepted.id
    db.commit()
    b.restore_backup(archive_path,archive_sha,actor='synthetic-workbook-store',reason='Synthetic full cycle archive recovery',passphrase='synthetic-only-return-cycle')
    db.expire_all()
    assert db.get(LeaveSyntheticRegisterConsumption,accepted_id) is not None
    assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==2
    assert db.scalar(select(func.count()).select_from(LeaveMonthClose))==0
    # Post-swap failure must restore current nonempty register consumption state.
    current=db.get(SystemSetting,'backup_root')
    marker=SystemSetting(key='synthetic-cycle-recovery-marker',value='CURRENT-AFTER-RECOVERY',updated_by='synthetic')
    db.add(marker);db.commit()
    def fail_finalize(**kw):raise RuntimeError('Synthetic 0062 post-swap failure')
    monkeypatch.setattr(b,'_finalize_restored_security',fail_finalize)
    with pytest.raises(RuntimeError,match='previous database was restored'):
        b.restore_backup(archive_path,archive_sha,actor='synthetic-workbook-store',reason='Synthetic 0062 rollback',passphrase='synthetic-only-return-cycle')
    db.expire_all()
    assert db.get(LeaveSyntheticRegisterConsumption,accepted_id) is not None
    assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==2
    assert db.get(SystemSetting,'synthetic-cycle-recovery-marker').value=='CURRENT-AFTER-RECOVERY'




@pytest.mark.parametrize('change',['disabled','unknown','existing','role','epoch','holiday','missingattendance'])
def test_material_register_refusals_no_partial_rows(history_context,monkeypatch,change):
    from app.config import settings
    from app.leave_synthetic_register import preview_synthetic_register,consume_synthetic_register
    from app.models import AdminAccount,LeaveHoliday,AttendanceEvidenceConsumption,LeaveSyntheticRegisterConsumption
    ctx=setup_return_chain(history_context,returned_code=None if change=='unknown' else 'P')
    db=ctx['db'];e=ctx['employee'];auth=ctx['auth'];comparison=ctx['compared']
    monkeypatch.setattr(settings,'leave_synthetic_register_enabled',True)
    monkeypatch.setattr(settings,'app_env','test')
    call=dict(operation_id='7'*64,comparison_id=comparison.id,employee_id=e.id,reviewed_digest='a'*64,human_confirmed=True,reason='Synthetic refusal',**auth)
    if change=='unknown':
        with pytest.raises(ValueError,match='explicit returned'):preview_synthetic_register(db,comparison_id=comparison.id,employee_id=e.id,**auth)
    else:
        _,digest=preview_synthetic_register(db,comparison_id=comparison.id,employee_id=e.id,**auth)
        call['reviewed_digest']=digest
        if change=='disabled':monkeypatch.setattr(settings,'leave_synthetic_register_enabled',False)
        elif change=='existing':db.add(LeaveRegisterEntry(employee_id=e.id,work_date=date(2026,5,31),code='',source='existing-synthetic',recorded_by='synthetic'));db.flush()
        elif change=='role':db.get(AdminAccount,auth['actor_id']).role='finance';db.flush()
        elif change=='epoch':call['expected_epoch']='stale'
        elif change=='holiday':db.add(LeaveHoliday(holiday_date=date(2026,5,30),name='Synthetic changed source',created_by='synthetic'));db.flush()
        elif change=='missingattendance':
            link=db.scalar(select(AttendanceEvidenceConsumption));db.delete(link);db.flush()
        count=db.scalar(select(func.count()).select_from(LeaveRegisterEntry))
        with pytest.raises(ValueError):consume_synthetic_register(db,**call)
        assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==count
    assert db.scalar(select(func.count()).select_from(LeaveSyntheticRegisterConsumption))==0


def test_explicit_leave_with_punches_refuses_not_absence_inference(history_context,monkeypatch):
    from app.leave_synthetic_register import preview_synthetic_register
    ctx=setup_return_chain(history_context,returned_code='A')
    with pytest.raises(ValueError,match='calendar punches'):
        preview_synthetic_register(ctx['db'],comparison_id=ctx['compared'].id,employee_id=ctx['employee'].id,**ctx['auth'])


def test_explicit_leave_without_same_day_punch_registers_no_fake_attendance(history_context,monkeypatch):
    from app.config import settings
    from app.leave_synthetic_register import preview_synthetic_register,consume_synthetic_register
    from app.models import AttendanceDailyRecord
    ctx=setup_return_chain(history_context,returned_code='A',punches=False)
    db=ctx['db'];e=ctx['employee'];auth=ctx['auth']
    monkeypatch.setattr(settings,'leave_synthetic_register_enabled',True)
    monkeypatch.setattr(settings,'app_env','test')
    before=db.scalar(select(func.count()).select_from(AttendanceDailyRecord))
    _,digest=preview_synthetic_register(db,comparison_id=ctx['compared'].id,employee_id=e.id,**auth)
    row=consume_synthetic_register(db,operation_id='6'*64,comparison_id=ctx['compared'].id,employee_id=e.id,
        reviewed_digest=digest,human_confirmed=True,reason='Synthetic explicit annual no punch',**auth)
    saved=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id))
    assert saved.code=='A' and saved.certificate_attached is None and saved.leave_record_id is None
    assert db.scalar(select(func.count()).select_from(AttendanceDailyRecord))==before


def test_replay_binds_reason_and_registered_revision(history_context,monkeypatch):
    from app.config import settings
    from app.leave_synthetic_register import preview_synthetic_register,consume_synthetic_register
    ctx=setup_return_chain(history_context,returned_code='A',punches=False)
    db=ctx['db'];e=ctx['employee'];auth=ctx['auth']
    monkeypatch.setattr(settings,'leave_synthetic_register_enabled',True)
    monkeypatch.setattr(settings,'app_env','test')
    _,digest=preview_synthetic_register(db,comparison_id=ctx['compared'].id,employee_id=e.id,**auth)
    call=dict(operation_id='5'*64,comparison_id=ctx['compared'].id,employee_id=e.id,reviewed_digest=digest,
        human_confirmed=True,reason='Synthetic explicit source',**auth)
    row=consume_synthetic_register(db,**call)
    assert consume_synthetic_register(db,**call).id==row.id
    with pytest.raises(ValueError,match='reason'):consume_synthetic_register(db,**{**call,'reason':'Changed'})
    saved=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id))
    saved.revision+=1;db.flush()
    with pytest.raises(ValueError,match='changed'):consume_synthetic_register(db,**call)


@pytest.mark.parametrize('second_code',['A',None,'S'])
def test_multi_employed_days_explicit_presence_leave_unknown_conflicts(history_context,monkeypatch,second_code):
    from app.config import settings
    from app.leave_synthetic_register import preview_synthetic_register,consume_synthetic_register
    from app.leave_payroll_read import get_month_summary
    from app.models import DepartmentFingerprintPreparation
    from hashlib import sha256
    import json
    ctx=setup_return_chain(history_context,multi=True,second_code=second_code)
    db=ctx['db'];e=ctx['employee'];auth=ctx['auth'];comparison=ctx['compared']
    monkeypatch.setattr(settings,'leave_synthetic_register_enabled',True)
    monkeypatch.setattr(settings,'app_env','test')
    if second_code is None:
        with pytest.raises(ValueError,match='explicit returned'):
            preview_synthetic_register(db,comparison_id=comparison.id,employee_id=e.id,**auth)
        assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==0
        return
    proposals,digest=preview_synthetic_register(db,comparison_id=comparison.id,employee_id=e.id,**auth)
    assert [(p['date'],p['code']) for p in proposals]==[('2026-05-30',''),('2026-05-31',second_code)]
    consumed=consume_synthetic_register(db,operation_id='4'*64,comparison_id=comparison.id,employee_id=e.id,
        reviewed_digest=digest,human_confirmed=True,reason='Synthetic two employed days explicit P and leave',**auth)
    assert len(json.loads(consumed.result_json)['ids'])==2
    months=range(1,6)
    summary=get_month_summary(db,employee_id=e.id,year=2026,month=5,selection_ids=(ctx['selected'].id,),department_id=e.department_id,
        month_start=date(2026,5,1),current_insurance_years={(e.id,date(2026,m,1)):None for m in months},
        age_evaluation_dates={date(2026,m,1):date(2026,m,1) for m in months},release_evaluated_on=date(2026,5,31),**auth)
    assert summary.employed_days==2 and summary.present_days==1 and dict(summary.codes)[second_code]==1
    assert summary.unknown_days==0 and all(r.available_balance is None for r in summary.balance_evidence)
