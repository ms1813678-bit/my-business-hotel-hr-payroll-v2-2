"""Real migrated synthetic transaction tests, rollback every case."""
import json
from dataclasses import replace
from datetime import date,time,datetime,UTC
from hashlib import sha256
import pytest
from sqlalchemy import select,func
from app.models import *
from app.department_manifest import encode_manifest
from app.department_workbook import build_department_workbook
from app.attendance_evidence_review import prepare_evidence_review,consume_evidence_review
from tests.test_leave_historical_evidence import history_context
from tests.test_department_workbook_store import context
from tests.test_department_workbook import make_sample


@pytest.fixture
def review_context(history_context):
    db,actor,auth,fact=history_context
    employee=db.get(Employee,fact.employee_id);department=db.get(Department,employee.department_id)
    _,manifest=make_sample(date(2026,11,1),1)
    person=replace(manifest.employees[0],employee_id=employee.id,code=employee.code,name=employee.full_name)
    from pathlib import Path
    _,manifest=build_department_workbook(department=department.name,month_start=date(2026,11,1),employees=(person,),approved_print_logo=Path('app/static/hotel-logo-print-approved.png'))
    body=encode_manifest(manifest)
    export=DepartmentWorkbookExport(id='e'*64,actor_id=actor.id,manifest_json=body,manifest_sha256=sha256(body.encode()).hexdigest(),source_digest='a'*64,restore_epoch=auth['expected_epoch'],created_at=datetime.now(UTC));db.add(export);db.flush()
    days=[dict(employee_id=employee.id,date='2026-11-01',code='P',raw_value='P',sheet='Synthetic',cell='C2')]
    body=json.dumps(days)
    receipt=DepartmentWorkbookPreparation(export_id=export.id,return_sha256='c'*64,actor_id=actor.id,evidence_json=body,evidence_sha256=sha256(body.encode()).hexdigest(),created_at=datetime.now(UTC));db.add(receipt);db.flush()
    raw=b'ID,Date,Time\nSYN-HISTORY,2026-11-01,23:00 23:00\nSYN-HISTORY,2026-11-02,07:00\n'
    review=json.dumps(dict(version=2,alias_bindings=[[employee.code,employee.code.casefold(),employee.id]],alias_bindings_sha256=sha256(json.dumps([[employee.code,employee.code.casefold(),employee.id]],ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),export_id=export.id,department_sha256=receipt.return_sha256,fingerprint_sha256=sha256(raw).hexdigest(),days=[dict(employee_id=employee.id,date='2026-11-01',department_code='P',sheet='Synthetic',cell='C2')]))
    source=DepartmentFingerprintPreparation(id='d'*64,return_id=receipt.id,actor_id=actor.id,fingerprint_bytes=raw,fingerprint_sha256=sha256(raw).hexdigest(),review_json=review,review_sha256=sha256(review.encode()).hexdigest(),restore_epoch=auth['expected_epoch'],created_at=datetime.now(UTC));db.add(source)
    pattern=ShiftPattern(department_id=department.id,code='SYN-NIGHT',name='Synthetic night',start_time=time(23),end_time=time(7),match_before_minutes=0,match_after_minutes=0,active=True);db.add(pattern);db.flush()
    db.add(EmployeeServicePeriod(employee_id=employee.id,start_date=date(2025,1,1)))
    db.add(AttendanceMonth(period_start=date(2026,11,1),period_end=date(2026,11,30),status='under_review',created_by=actor.username));db.flush()
    args=dict(operation_id='f'*64,comparison_id=source.id,employee_id=employee.id,work_date=date(2026,11,1),windows=((date(2026,11,1),pattern.id),),selected_window=f'2026-11-01:{pattern.id}',in_ordinal=0,out_ordinal=2,reason='Explicit synthetic endpoint direction review',**auth)
    return db,actor,auth,source,export,receipt,pattern,args


def count(db,model):return db.scalar(select(func.count()).select_from(model))


def test_pending_atomic_consumption_exact_replay_and_no_financial_effect(review_context):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    financial=tuple(count(db,m) for m in (LeaveBalanceTransaction,PayrollItem))
    n=count(db,AttendanceDailyRecord);a=count(db,AuditLog)
    with db.begin_nested() as tx:
        row=prepare_evidence_review(db,**args)
        assert prepare_evidence_review(db,**args).id==row.id
        assert count(db,AttendanceDailyRecord)==n and count(db,AuditLog)==a+1
        record=consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
        assert record.status=='needs_review' and record.worked_minutes==480
        assert record.scheduled_start_at is None and record.approved_clock_in is None and record.first_clock_in is None
        assert count(db,AttendanceDailyRecord)==n+1
        assert consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth).id==record.id
        assert prepare_evidence_review(db,**args).id==row.id
        assert count(db,AuditLog)==a+2 and count(db,AttendanceEvidenceConsumption)==1
        assert financial==tuple(count(db,m) for m in (LeaveBalanceTransaction,PayrollItem))
        tx.rollback()
    assert count(db,AttendanceDailyRecord)==n and count(db,AttendanceEvidenceReviewReceipt)==0


@pytest.mark.parametrize('change',['reason','ordinal','epoch','session','role','config','raw','identity','invalidated','exportbinding','days','malformed','closedattendance','existing','fingerprintalias','malformedalias'])
def test_freshness_and_request_refusals(review_context,change):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    row=prepare_evidence_review(db,**args)
    before=count(db,AttendanceDailyRecord)
    if change=='reason':args['reason']='Changed request'
    elif change=='ordinal':args['out_ordinal']=1
    elif change=='epoch':args['expected_epoch']='stale'
    elif change=='session':args['session_version']+=1
    elif change=='role':actor.role='finance'
    elif change=='config':pattern.name='Changed config'
    elif change=='raw':source.fingerprint_bytes+=b'\n'
    elif change=='identity':db.get(Employee,args['employee_id']).full_name='Changed identity'
    elif change=='invalidated':export.invalidated_at=datetime.now(UTC)
    elif change in ('exportbinding','days','malformed'):
        data=json.loads(source.review_json)
        if change=='exportbinding':data['export_id']='0'*64
        elif change=='days':data['days']=[]
        else:data=[]
        source.review_json=json.dumps(data);source.review_sha256=sha256(source.review_json.encode()).hexdigest()
    elif change=='closedattendance':db.scalar(select(AttendanceMonth).where(AttendanceMonth.period_start==date(2026,11,1))).status='approved'
    elif change=='fingerprintalias':db.get(Employee,args['employee_id']).fingerprint_id='NEW-IDENTITY'
    elif change=='malformedalias':
        data=json.loads(source.review_json);data['alias_bindings']=[[]]
        source.review_json=json.dumps(data);source.review_sha256=sha256(source.review_json.encode()).hexdigest()
    elif change=='existing':db.add(AttendanceDailyRecord(employee_id=args['employee_id'],work_date=args['work_date']))
    db.flush()
    with pytest.raises(ValueError):prepare_evidence_review(db,**args)
    assert count(db,AttendanceDailyRecord)==before+(change=='existing')
    assert count(db,AttendanceEvidenceConsumption)==0


@pytest.mark.parametrize('updates',[dict(out_ordinal=99),dict(in_ordinal=True),dict(in_ordinal=2,out_ordinal=0),dict(out_ordinal=1),dict(reason=' '),dict(work_date=date(2026,11,2)),dict(employee_id=True)])
def test_explicit_endpoint_intent_bounds(review_context,updates):
    db,*_,args=review_context
    with pytest.raises(ValueError):prepare_evidence_review(db,**{**args,**updates})
    assert count(db,AttendanceEvidenceReviewReceipt)==0


@pytest.mark.parametrize('change',['config','consumedrecord','confirm','storedproposal','receiptidentity','sessiondelete','epochsetting','newexport','department'])
def test_consume_revalidates_and_atomic_rollback(review_context,change):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    row=prepare_evidence_review(db,**args)
    if change=='config':pattern.name='Changed after preparation'
    elif change=='consumedrecord':
        record=consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
        record.worked_minutes=1
    elif change=='confirm':pass
    elif change=='storedproposal':row.proposal_json='[]'
    elif change=='receiptidentity':row.employee_id+=10000
    elif change=='sessiondelete':db.delete(db.get(AdminSession,auth['session_id']))
    elif change=='epochsetting':db.add(SystemSetting(key='restore_epoch',value='synthetic-restored-epoch',updated_by='synthetic-test'))
    elif change=='newexport':receipt.export_id=context_export=db.scalar(select(DepartmentWorkbookExport.id).where(DepartmentWorkbookExport.id!=export.id))
    elif change=='department':db.get(Department,pattern.department_id).name='Changed department'
    # Identity tamper uses ORM fields only inside rolled-back test, avoid nonexistent FK.
    if change=='receiptidentity':row.employee_id=args['employee_id'];row.work_date=date(2026,11,2)
    db.flush()
    before=count(db,AttendanceDailyRecord);links=count(db,AttendanceEvidenceConsumption)
    with pytest.raises(ValueError):consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest='0'*64 if change=='confirm' else row.request_digest,**auth)
    assert count(db,AttendanceDailyRecord)==before and count(db,AttendanceEvidenceConsumption)==links


@pytest.mark.parametrize('field,value',[('session_id',[]),('session_id',''),('session_version',True),('actor_id',True),('expected_epoch',{} )])
def test_malformed_identity_controlled(review_context,field,value):
    db,*_,args=review_context
    with pytest.raises(ValueError):prepare_evidence_review(db,**{**args,field:value})


def test_nonascii_reason_and_current_session_supported(review_context):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    old=db.get(AdminSession,auth['session_id']);db.delete(old);db.flush()
    sid='جلسة-合成'
    db.add(AdminSession(id=sid,admin_account_id=actor.id));db.flush()
    row=prepare_evidence_review(db,**{**args,'session_id':sid,'reason':'مراجعة اتجاه البصمة 合成'})
    assert json.loads(row.proposal_json)['reason']=='مراجعة اتجاه البصمة 合成'


@pytest.mark.parametrize('kind',['leave','payroll'])
def test_closed_period_refuses_actual_rows(review_context,kind):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    if kind=='leave':
        backup=BackupRun(backup_type='manual',status='success',size_bytes=0,initiated_by=actor.username);db.add(backup);db.flush()
        db.add(LeaveMonthClose(year=2026,month=11,backup_run_id=backup.id,closed_by=actor.username,closed_at=datetime.now(UTC),policy_snapshot_json='{}'))
    else:db.add(PayrollRun(period_start=date(2026,11,1),period_end=date(2026,11,30),pay_date=date(2026,12,1),status='closed',created_by=actor.username))
    db.flush()
    with pytest.raises(ValueError):prepare_evidence_review(db,**args)
    assert count(db,AttendanceEvidenceReviewReceipt)==0


def test_injected_failure_rolls_back_attendance_consumption_audit_together(review_context):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    row=prepare_evidence_review(db,**args);rid=row.id;request=row.request_digest
    before=tuple(count(db,m) for m in (AttendanceDailyRecord,AttendanceEvidenceConsumption,AuditLog))
    with pytest.raises(RuntimeError):
        with db.begin_nested():
            consume_evidence_review(db,receipt_id=rid,confirmed_request_digest=request,**auth)
            raise RuntimeError('Synthetic failure before transaction commit')
    assert before==tuple(count(db,m) for m in (AttendanceDailyRecord,AttendanceEvidenceConsumption,AuditLog))
    assert consume_evidence_review(db,receipt_id=rid,confirmed_request_digest=request,**auth).status=='needs_review'


def test_same_original_occurrence_cannot_back_different_workday(review_context):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    # Two explicit saved P workdays; no inference that overlapping windows are assignments.
    from datetime import time
    days=json.loads(receipt.evidence_json)
    days.append({**days[0],'date':'2026-11-02','cell':'D2'})
    receipt.evidence_json=json.dumps(days);receipt.evidence_sha256=sha256(receipt.evidence_json.encode()).hexdigest()
    review=json.loads(source.review_json);review['days'].append({**review['days'][0],'date':'2026-11-02','cell':'D2'})
    raw=b'ID,Date,Time\nSYN-HISTORY,2026-11-02,01:00 02:00\n'
    source.fingerprint_bytes=raw;source.fingerprint_sha256=sha256(raw).hexdigest()
    review['fingerprint_sha256']=source.fingerprint_sha256
    source.review_json=json.dumps(review);source.review_sha256=sha256(source.review_json.encode()).hexdigest()
    other=ShiftPattern(department_id=pattern.department_id,code='SYN-MORNING',name='Synthetic overlapping morning',start_time=time(0),end_time=time(8),match_before_minutes=0,match_after_minutes=0,active=True);db.add(other);db.flush()
    first={**args,'out_ordinal':1}
    second={**first,'operation_id':'2'*64,'work_date':date(2026,11,2),'windows':((date(2026,11,2),other.id),),'selected_window':f'2026-11-02:{other.id}'}
    one=prepare_evidence_review(db,**first);two=prepare_evidence_review(db,**second)
    consume_evidence_review(db,receipt_id=one.id,confirmed_request_digest=one.request_digest,**auth)
    assert count(db,AttendanceEvidenceOccurrenceClaim)==2
    with pytest.raises(ValueError,match='already used'):
        consume_evidence_review(db,receipt_id=two.id,confirmed_request_digest=two.request_digest,**auth)
    assert count(db,AttendanceDailyRecord)==1 and count(db,AttendanceEvidenceConsumption)==1
    assert count(db,AttendanceEvidenceOccurrenceClaim)==2


def test_occurrence_claims_roll_back_with_consumption(review_context):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    row=prepare_evidence_review(db,**args)
    with pytest.raises(RuntimeError):
        with db.begin_nested():
            consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
            assert count(db,AttendanceEvidenceOccurrenceClaim)==2
            raise RuntimeError('Synthetic rollback')
    assert count(db,AttendanceEvidenceOccurrenceClaim)==0
    assert count(db,AttendanceDailyRecord)==0
    consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
    assert count(db,AttendanceEvidenceOccurrenceClaim)==2


@pytest.mark.parametrize('kind',['unknown','unreadable'])
def test_new_bridge_refuses_unresolved_source_before_receipt(review_context,kind):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    raw=source.fingerprint_bytes+(b'UNKNOWN,2026-11-01,23:30\n' if kind=='unknown' else b'SYN-HISTORY,2026-11-01,broken-time\n')
    source.fingerprint_bytes=raw;source.fingerprint_sha256=sha256(raw).hexdigest()
    review=json.loads(source.review_json);review['fingerprint_sha256']=source.fingerprint_sha256
    source.review_json=json.dumps(review);source.review_sha256=sha256(source.review_json.encode()).hexdigest();db.flush()
    with pytest.raises(ValueError,match='Unresolved or unreadable'):prepare_evidence_review(db,**args)
    assert count(db,AttendanceEvidenceReviewReceipt)==0 and count(db,AttendanceDailyRecord)==0


@pytest.mark.parametrize('change',['no_month','imported_month','overlap_month','no_service','ended_service','changed_service'])
def test_legacy_service_and_review_month_prerequisites(review_context,change):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    if change=='changed_service':row=prepare_evidence_review(db,**args)
    month=db.scalar(select(AttendanceMonth).where(AttendanceMonth.period_start==date(2026,11,1)))
    service=db.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==args['employee_id']))
    if change=='no_month':db.delete(month)
    elif change=='imported_month':month.status='imported'
    elif change=='overlap_month':db.add(AttendanceMonth(period_start=date(2026,11,1),period_end=date(2026,11,29),status='under_review',created_by=actor.username))
    elif change=='no_service':db.delete(service)
    elif change=='ended_service':service.end_date=date(2026,10,31)
    elif change=='changed_service':service.start_date=date(2025,2,1)
    db.flush()
    with pytest.raises(ValueError):prepare_evidence_review(db,**args)
    assert count(db,AttendanceDailyRecord)==0


def test_parser_budget_failure_has_zero_pending_or_consumption_effect(review_context,monkeypatch):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    import app.fingerprint_bounded_parse as module
    def refuse(*args,**kwargs):raise ValueError('Synthetic parser safety budget exceeded.')
    before=tuple(count(db,m) for m in (AttendanceEvidenceReviewReceipt,AttendanceDailyRecord,AttendanceEvidenceConsumption,AttendanceEvidenceOccurrenceClaim,AuditLog))
    with monkeypatch.context() as patch:
        patch.setattr(module,'parse_operational_fingerprint',refuse)
        with pytest.raises(ValueError,match='budget'):prepare_evidence_review(db,**args)
    assert before==tuple(count(db,m) for m in (AttendanceEvidenceReviewReceipt,AttendanceDailyRecord,AttendanceEvidenceConsumption,AttendanceEvidenceOccurrenceClaim,AuditLog))
    row=prepare_evidence_review(db,**args)
    before=tuple(count(db,m) for m in (AttendanceDailyRecord,AttendanceEvidenceConsumption,AttendanceEvidenceOccurrenceClaim,AuditLog))
    with monkeypatch.context() as patch:
        patch.setattr(module,'parse_operational_fingerprint',refuse)
        with pytest.raises(ValueError,match='budget'):consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
    assert before==tuple(count(db,m) for m in (AttendanceDailyRecord,AttendanceEvidenceConsumption,AttendanceEvidenceOccurrenceClaim,AuditLog))


@pytest.mark.parametrize('change',['monthapproved','monthdelete','serviceend','servicedelete'])
def test_consumption_rechecks_postprepare_month_service_changes(review_context,change):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    row=prepare_evidence_review(db,**args)
    month=db.scalar(select(AttendanceMonth).where(AttendanceMonth.period_start==date(2026,11,1)))
    service=db.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==args['employee_id']))
    if change=='monthapproved':month.status='approved'
    elif change=='monthdelete':db.delete(month)
    elif change=='serviceend':service.end_date=date(2026,10,31)
    elif change=='servicedelete':db.delete(service)
    db.flush()
    with pytest.raises(ValueError):consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
    assert count(db,AttendanceDailyRecord)==0 and count(db,AttendanceEvidenceConsumption)==0 and count(db,AttendanceEvidenceOccurrenceClaim)==0


def test_same_legacy_manual_prerequisites_without_mutating_legacy_record(review_context):
    from app.attendance_workflow import add_manual_attendance
    db,actor,auth,source,export,receipt,pattern,args=review_context
    with db.begin_nested() as tx:
        record=add_manual_attendance(db,employee_id=args['employee_id'],work_date=args['work_date'],clock_in=datetime(2026,11,1,23),clock_out=datetime(2026,11,2,7),reason='Synthetic differential endpoints',actor=actor.username)
        assert record.status=='needs_review' and record.worked_minutes==480
        tx.rollback()
    row=prepare_evidence_review(db,**args)
    record=consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
    assert record.status=='needs_review' and record.worked_minutes==480


@pytest.mark.parametrize('changed_after_prepare',[False,True])
def test_exact_existing_month_batch_ids_or_refuse_changed_snapshot(review_context,changed_after_prepare):
    db,actor,auth,source,export,receipt,pattern,args=review_context
    month=db.scalar(select(AttendanceMonth).where(AttendanceMonth.period_start==date(2026,11,1)))
    def batch(kind,hashchar):
        row=AttendanceImportBatch(source_type=kind,period_start=date(2026,11,1),period_end=date(2026,11,30),original_filename='synthetic.csv',file_sha256=hashchar*64,uploaded_by=actor.username)
        db.add(row);db.flush();return row.id
    program=batch('program_report','1');fingerprint=batch('fingerprint','2')
    month.program_import_batch_id=program;month.fingerprint_import_batch_id=fingerprint;db.flush()
    row=prepare_evidence_review(db,**args)
    if changed_after_prepare:
        month.fingerprint_import_batch_id=batch('fingerprint','3');db.flush()
        with pytest.raises(ValueError):consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
        assert count(db,AttendanceDailyRecord)==0
    else:
        record=consume_evidence_review(db,receipt_id=row.id,confirmed_request_digest=row.request_digest,**auth)
        assert record.program_import_batch_id==program and record.fingerprint_import_batch_id==fingerprint
        assert json.loads(record.review_notes)['receipt_id']==row.id


def test_unsupported_parser_platform_has_zero_persisted_effects(review_context,monkeypatch):
    from app import fingerprint_bounded_parse as parser
    db,actor,auth,source,export,receipt,pattern,args=review_context
    models=(AttendanceEvidenceReviewReceipt,AttendanceEvidenceConsumption,AttendanceDailyRecord,AuditLog,LeaveBalanceTransaction,PayrollItem)
    before=tuple(count(db,model) for model in models)
    monkeypatch.setattr(parser,'_operational_platform',lambda:'nt')
    monkeypatch.setattr(parser.subprocess,'run',lambda *a,**k:pytest.fail('unsupported platform must not launch parser'))
    with pytest.raises(parser.FingerprintPlatformUnavailable):
        prepare_evidence_review(db,**args)
    assert tuple(count(db,model) for model in models)==before
    assert not db.new and not db.dirty and not db.deleted
