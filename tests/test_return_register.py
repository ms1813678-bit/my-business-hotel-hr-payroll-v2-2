"""Actual migrated source/kernel checks, no balance or payroll effects."""
from datetime import date
import pytest
from sqlalchemy import select,func,event,text
from app.leave_return_register import preview_return_register,consume_return_register
from app.models import (LeaveRegisterEntry,LeaveRegisterConsumption,LeaveBalanceTransaction,
    EmployeeServicePeriod,AuditLog,AdminAccount,LeaveMonthClose)
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_return_register_cycle import setup_return_chain



def unsupported_boundary(ctx):
    import os
    if os.name in ('posix','nt'):return False
    from app.fingerprint_bounded_parse import FingerprintPlatformUnavailable
    db=ctx['db']
    with pytest.raises(FingerprintPlatformUnavailable):
        preview_return_register(db,comparison_id=ctx['compared'].id,employee_id=ctx['employee'].id,**ctx['auth'])
    assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==0
    assert db.scalar(select(func.count()).select_from(LeaveRegisterConsumption))==0
    return True

def invocation(ctx,digest):
    return dict(operation_id='a'*64,comparison_id=ctx['compared'].id,
        employee_id=ctx['employee'].id,reviewed_digest=digest,human_confirmed=True,
        reason='Synthetic exact normal receipt',**ctx['auth'])


@pytest.mark.parametrize('code',['A','W','H','S','U'])
def test_explicit_nonattendance_days_record_exactly_with_no_ledger_effect(history_context,code):
    ctx=setup_return_chain(history_context,returned_code=code,punches=False)
    if unsupported_boundary(ctx):return
    db=ctx['db'];before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    proposals,digest=preview_return_register(db,comparison_id=ctx['compared'].id,employee_id=ctx['employee'].id,**ctx['auth'])
    assert [p['code'] for p in proposals]==[code]
    receipt=consume_return_register(db,**invocation(ctx,digest))
    assert receipt.id==consume_return_register(db,**invocation(ctx,digest)).id
    day=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==ctx['employee'].id))
    assert day.code==code and day.source=='reviewed-return' and day.leave_record_id is None
    assert day.certificate_attached is None and day.notes is None
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before
    with pytest.raises(ValueError,match='already consumed'):
        consume_return_register(db,**{**invocation(ctx,digest),'operation_id':'b'*64})


@pytest.mark.parametrize('case',['unknown','p_without_receipt','leave_with_punches','existing','service_overlap','closed'])
def test_unresolved_day_or_collision_refuses_atomically(history_context,case):
    code=None if case=='unknown' else 'P' if case=='p_without_receipt' else 'A'
    ctx=setup_return_chain(history_context,returned_code=code,punches=case=='leave_with_punches',consume_attendance=False)
    if unsupported_boundary(ctx):return
    db=ctx['db'];e=ctx['employee']
    if case=='existing':db.add(LeaveRegisterEntry(employee_id=e.id,work_date=e.hire_date,code='W',source='existing',revision=1,recorded_by='synthetic'))
    if case=='service_overlap':db.add(EmployeeServicePeriod(employee_id=e.id,start_date=e.hire_date,end_date=e.hire_date))
    if case=='closed':
        from app.models import BackupRun,utcnow
        backup=BackupRun(backup_type='manual',status='success',size_bytes=0,initiated_by='synthetic');db.add(backup);db.flush()
        db.add(LeaveMonthClose(year=2026,month=5,backup_run_id=backup.id,closed_at=utcnow(),closed_by='synthetic',policy_snapshot_json='{}'))
    db.flush()
    before=db.scalar(select(func.count()).select_from(LeaveRegisterEntry))
    with pytest.raises(ValueError):
        preview_return_register(db,comparison_id=ctx['compared'].id,employee_id=e.id,**ctx['auth'])
    assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==before
    assert db.scalar(select(func.count()).select_from(LeaveRegisterConsumption))==0


@pytest.mark.parametrize('change',['digest','actor','session','epoch','confirm','reason','employee','operation'])
def test_exact_review_and_authority_required(history_context,change):
    ctx=setup_return_chain(history_context,returned_code='A',punches=False)
    if unsupported_boundary(ctx):return
    db=ctx['db'];_,digest=preview_return_register(db,comparison_id=ctx['compared'].id,employee_id=ctx['employee'].id,**ctx['auth'])
    call=invocation(ctx,digest)
    values={'digest':('reviewed_digest','b'*64),'actor':('actor_id',2147483647),'session':('session_id','missing'),
        'epoch':('expected_epoch','changed'),'confirm':('human_confirmed',False),'reason':('reason',' '),
        'employee':('employee_id',True),'operation':('operation_id','bad')}
    key,value=values[change];call[key]=value
    with pytest.raises((ValueError,TypeError)):consume_return_register(db,**call)
    assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==0
    assert db.scalar(select(func.count()).select_from(LeaveRegisterConsumption))==0


def test_audit_failure_rolls_back_all_days_and_receipt(history_context):
    ctx=setup_return_chain(history_context,returned_code='A',punches=False)
    if unsupported_boundary(ctx):return
    db=ctx['db'];_,digest=preview_return_register(db,comparison_id=ctx['compared'].id,employee_id=ctx['employee'].id,**ctx['auth'])
    def fail(session,*args):
        if any(isinstance(row,AuditLog) and row.action=='reviewed_return_registered' for row in session.new):raise ValueError('Synthetic audit failure')
    event.listen(db,'before_flush',fail)
    try:
        with pytest.raises(ValueError,match='Synthetic audit failure'):consume_return_register(db,**invocation(ctx,digest))
    finally:event.remove(db,'before_flush',fail)
    assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==0
    assert db.scalar(select(func.count()).select_from(LeaveRegisterConsumption))==0


def test_committed_receipt_replay_survives_later_source_edits_but_not_result_edit(history_context):
    ctx=setup_return_chain(history_context,returned_code='A',punches=False)
    if unsupported_boundary(ctx):return
    db=ctx['db'];_,digest=preview_return_register(db,comparison_id=ctx['compared'].id,employee_id=ctx['employee'].id,**ctx['auth'])
    call=invocation(ctx,digest);saved=consume_return_register(db,**call)
    # A subsequent legitimate source edit is not a false failure for an already completed operation.
    ctx['employee'].full_name='Synthetic renamed after registration';db.flush()
    assert consume_return_register(db,**call).id==saved.id
    day=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==ctx['employee'].id))
    day.code='W';day.revision+=1;db.flush()
    with pytest.raises(ValueError,match='receipt/result changed'):consume_return_register(db,**call)


def test_mixed_source_month_refuses_before_first_month_selection(history_context):
    import json
    from hashlib import sha256
    from app.models import DepartmentWorkbookPreparation
    ctx=setup_return_chain(history_context,returned_code='A',punches=False)
    if unsupported_boundary(ctx):return
    db=ctx['db'];compared=ctx['compared']
    returned=db.get(DepartmentWorkbookPreparation,compared.return_id)
    evidence=json.loads(returned.evidence_json);evidence[-1]['date']='2026-06-01'
    returned.evidence_json=json.dumps(evidence);returned.evidence_sha256=sha256(returned.evidence_json.encode()).hexdigest()
    review=json.loads(compared.review_json)
    review['days']=[dict(employee_id=v['employee_id'],date=v['date'],department_code=v['code'],sheet=v['sheet'],cell=v['cell']) for v in evidence]
    compared.review_json=json.dumps(review);compared.review_sha256=sha256(compared.review_json.encode()).hexdigest();db.flush()
    with pytest.raises(ValueError,match='Unique saved employee month days required'):
        preview_return_register(db,comparison_id=compared.id,employee_id=ctx['employee'].id,**ctx['auth'])
    assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==0
