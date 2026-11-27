"""Migrated PostgreSQL acceptance, synthetic source/identities only."""
from datetime import date
from dataclasses import replace
import json,os
import pytest
from fastapi import HTTPException
from sqlalchemy import select,func,text,event
from sqlalchemy.exc import DBAPIError
from app.models import (LeaveReconciliationApproval,LeaveReconciliationReviewReceipt,LeaveType,
                        LeaveRegisterEntry,LeaveBalanceTransaction,AuditLog,AdminAccount,AdminSession)
from app.leave_reconciliation_source import reconciliation_source
from app.leave_source_reconciliation import reconcile_selected_opening
from app.leave_transition_draft import draft_transition,OpeningReleaseDecision,LedgerDecision
from app.leave_reconciliation_receipt import prepare_assertion_receipt
from app.leave_reconciliation_approval import approve_reconciliation,review_approval,approval_hash
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_return_register_cycle import setup_return_chain


def fixture(history_context):
    ctx=setup_return_chain(history_context,returned_code='A',consume_attendance=False)
    db=ctx['db'];e=ctx['employee'];auth=ctx['auth']
    for kind in db.scalars(select(LeaveType).where(LeaveType.code.in_(('annual','weekly_rest','official_holiday')))):
        kind.tracks_balance=True
    db.add(LeaveRegisterEntry(employee_id=e.id,work_date=date(2026,5,31),code='A',source='synthetic',recorded_by='synthetic'));db.flush()
    selection,_,_=reconciliation_source(db,ctx['compared'].id,e.id,auth)
    selection['release_evaluated_on']=date(2026,5,31)
    selection.update(auth)
    receipt=make_receipt(db,ctx,e,selection,'a'*64)
    return db,ctx,e,selection,receipt,dict(operation_id='b'*64,receipt_id=receipt.id,employee_id=e.id,
        reviewed_comparison_digest=reconciliation_source(db,ctx['compared'].id,e.id,ctx['auth'])[2].source_digest,reviewed_receipt_digest=receipt.integrity_digest,predecessor_id=None,reason='Synthetic exact HR approval',human_confirmed=True,**selection)


def make_receipt(db,ctx,e,selection,op):
    source=reconcile_selected_opening(db,**selection)
    ld=tuple(LedgerDecision(i,'external_to_scenario','Synthetic explicit external') for r in source.rows for i in r.ledger_row_ids)
    od=tuple(OpeningReleaseDecision(e.id,r.code,True,'Synthetic release evidence assertion') for r in source.rows)
    draft=draft_transition(db,reviewed_reconciliation_digest=source.reconciliation_digest,ledger_decisions=ld,opening_decisions=od,**selection)
    return prepare_assertion_receipt(db,operation_id=op,comparison_id=ctx['compared'].id,employee_id=e.id,
        reviewed_source_digest=source.reconciliation_digest,reviewed_decision_digest=draft.decision_digest,
        ledger_decisions=ld,opening_decisions=od,reason='Synthetic assertions not independent fact',human_confirmed=True,**selection)


def counts(db):
    return tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveReconciliationApproval,AuditLog,LeaveBalanceTransaction,LeaveRegisterEntry))


def test_exact_approval_replay_no_posting_or_duplicate_baseline(history_context):
    db,ctx,e,selection,receipt,call=fixture(history_context)
    before=counts(db)
    review=review_approval(db,receipt_id=receipt.id,employee_id=e.id,**selection)
    assert review[2:] == (None,None) and counts(db)==before
    row=approve_reconciliation(db,**call)
    assert approval_hash(row)==row.integrity_digest
    assert counts(db)==(before[0]+1,before[1]+1,*before[2:])
    after=counts(db)
    assert approve_reconciliation(db,**call).id==row.id and counts(db)==after
    with pytest.raises(ValueError,match='request changed'):approve_reconciliation(db,**{**call,'reason':'Different'})
    with pytest.raises(ValueError):approve_reconciliation(db,**{**call,'operation_id':'c'*64})
    assert review_approval(db,receipt_id=receipt.id,employee_id=e.id,**selection)[2].id==row.id
    for statement in ('UPDATE leave_reconciliation_approvals SET reason=reason','DELETE FROM leave_reconciliation_approvals'):
        with pytest.raises(DBAPIError):
            with db.begin_nested():db.execute(text(statement))
    audit=db.scalar(select(AuditLog).where(AuditLog.action=='leave_reconciliation_approved'))
    assert json.loads(audit.new_value)['posted'] is False


@pytest.mark.parametrize('change',['finance','assistant_manager','disabled','password','pending','version','session','epoch','confirmation','reason','digest','employee','dirty'])
def test_approval_current_authority_negative(history_context,change):
    db,ctx,e,selection,receipt,call=fixture(history_context);before=counts(db)
    account=db.get(AdminAccount,call['actor_id'])
    if change in ('finance','assistant_manager'):account.role=change
    elif change=='disabled':account.active=False
    elif change=='password':account.must_change_password=True
    elif change=='pending':account.pending_credential_reset=True
    elif change=='version':account.session_version+=1
    elif change=='session':db.delete(db.get(AdminSession,call['session_id']))
    elif change=='epoch':call['expected_epoch']='old'
    elif change=='confirmation':call['human_confirmed']=False
    elif change=='reason':call['reason']=' '
    elif change=='digest':call['reviewed_receipt_digest']='d'*64
    elif change=='employee':call['employee_id']+=1
    elif change=='dirty':e.full_name='Synthetic pending unflushed'
    if change!='dirty':db.flush()
    with pytest.raises(ValueError):approve_reconciliation(db,**call)
    assert counts(db)==before
    if change=='dirty':assert e in db.dirty


@pytest.mark.parametrize('boundary',['approval','audit'])
def test_injected_atomic_failure(history_context,boundary):
    db,ctx,e,selection,receipt,call=fixture(history_context);before=counts(db)
    def fail(session,*_):
        if any(isinstance(row,LeaveReconciliationApproval) if boundary=='approval' else isinstance(row,AuditLog) and row.action=='leave_reconciliation_approved' for row in session.new):
            raise ValueError('Synthetic injected failure')
    event.listen(db,'before_flush',fail)
    try:
        with pytest.raises(ValueError,match='injected'):approve_reconciliation(db,**call)
    finally:event.remove(db,'before_flush',fail)
    assert counts(db)==before
    assert approve_reconciliation(db,**call).id==call['operation_id']


def test_source_change_keeps_history_and_explicit_correction(history_context):
    db,ctx,e,selection,receipt,call=fixture(history_context)
    first=approve_reconciliation(db,**call)
    register=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id))
    register.code='W';db.flush()
    with pytest.raises(ValueError):review_approval(db,receipt_id=receipt.id,employee_id=e.id,**selection)
    # Exact retry is historical, never implies current applicability.
    assert approve_reconciliation(db,**call).id==first.id
    revised=make_receipt(db,ctx,e,selection,'c'*64)
    review=review_approval(db,receipt_id=revised.id,employee_id=e.id,**selection)
    assert review[2] is None and review[3]==first.id
    new={**call,'operation_id':'d'*64,'receipt_id':revised.id,'reviewed_receipt_digest':revised.integrity_digest}
    with pytest.raises(ValueError):approve_reconciliation(db,**new)
    second=approve_reconciliation(db,**{**new,'predecessor_id':first.id})
    assert second.predecessor_id==first.id and approval_hash(first)==first.integrity_digest
    with pytest.raises(ValueError):approve_reconciliation(db,**{**new,'operation_id':'e'*64,'predecessor_id':first.id})


def test_source_stale_before_first_acceptance_no_writes(history_context):
    db,ctx,e,selection,receipt,call=fixture(history_context)
    register=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id));register.code='W';db.flush()
    before=counts(db)
    with pytest.raises(ValueError):approve_reconciliation(db,**call)
    assert counts(db)==before


@pytest.mark.skipif(os.getenv('MANUAL_APPROVAL_CONCURRENCY')!='1',reason='Dedicated committed synthetic DB')
def test_independent_sessions_race(history_context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.db import SessionLocal
    db,ctx,e,selection,receipt,call=fixture(history_context);db.commit()
    def race(ids):
        barrier=Barrier(2)
        def run(op):
            with SessionLocal() as other:
                barrier.wait(5)
                try:
                    row=approve_reconciliation(other,**{**call,'operation_id':op});identity=row.id;other.commit();return identity
                except ValueError:other.rollback();return 'refused'
                except HTTPException as exc:
                    # Intentional fail-closed gate (auth gate try-lock): a contended request is refused
                    # promptly with 503 and Retry-After, never queued behind the lock.
                    other.rollback()
                    assert exc.status_code==503 and exc.headers.get('Retry-After')=='5'
                    return 'busy'
        with ThreadPoolExecutor(2) as pool:return list(pool.map(run,ids))
    first=race(('b'*64,'c'*64))
    # Exactly one approval can exist; the other call is either refused by the rule or busy at the gate.
    assert sum(x not in ('refused','busy') for x in first)<=1 and sum(x in ('refused','busy') for x in first)>=1
    with SessionLocal() as check:
        winner=check.scalar(select(LeaveReconciliationApproval.id))
        assert check.scalar(select(func.count()).select_from(LeaveReconciliationApproval))==1
    # Same operation id replayed in two sessions: each call returns the winner or is refused as busy,
    # and a retry of any busy call returns the same winner.
    again=race((winner,winner))
    assert all(x in (winner,'busy') for x in again)
    for _ in range(2):  # both sessions can be busy at once; the replay must still return the winner when not contended
        if winner in again:break
        again=race((winner,winner));assert all(x in (winner,'busy') for x in again)
    assert winner in again
    with SessionLocal() as retry:
        replay=approve_reconciliation(retry,**{**call,'operation_id':winner});replay_id=replay.id;retry.commit()
    assert replay_id==winner


@pytest.mark.parametrize('blocker',['close','payroll_midmonth'])
def test_closed_or_overlapping_payroll_blocks(history_context,blocker):
    from app.models import LeaveMonthClose,PayrollRun,BackupRun
    from datetime import datetime,UTC
    db,ctx,e,selection,receipt,call=fixture(history_context)
    if blocker=='close':
        backup=BackupRun(backup_type='manual',status='success',size_bytes=0,initiated_by='synthetic');db.add(backup);db.flush()
        db.add(LeaveMonthClose(year=2026,month=5,backup_run_id=backup.id,closed_at=datetime.now(UTC),policy_snapshot_json='{}',closed_by='synthetic'))
    else:
        db.add(PayrollRun(period_start=date(2026,5,15),period_end=date(2026,5,20),pay_date=date(2026,5,31),status='approved',created_by='synthetic'))
    db.flush();before=counts(db)
    with pytest.raises(ValueError):approve_reconciliation(db,**call)
    assert counts(db)==before


def test_snapshot_tuple_and_comparison_digest_bound(history_context):
    db,ctx,e,selection,receipt,call=fixture(history_context);before=counts(db)
    for key,value in [('month_start',date(2026,6,1)),('release_evaluated_on',date(2026,6,1)),('reviewed_comparison_digest','f'*64)]:
        with pytest.raises(ValueError):approve_reconciliation(db,**{**call,key:value})
    assert counts(db)==before


def test_unresolved_receipt_cannot_be_approved(history_context):
    db,ctx,e,selection,receipt,call=fixture(history_context)
    source=reconcile_selected_opening(db,**selection)
    od=tuple(OpeningReleaseDecision(e.id,r.code,None,'Synthetic unknown') for r in source.rows)
    draft=draft_transition(db,reviewed_reconciliation_digest=source.reconciliation_digest,ledger_decisions=(),opening_decisions=od,**selection)
    unknown=prepare_assertion_receipt(db,operation_id='e'*64,comparison_id=ctx['compared'].id,employee_id=e.id,
        reviewed_source_digest=source.reconciliation_digest,reviewed_decision_digest=draft.decision_digest,
        ledger_decisions=(),opening_decisions=od,reason='Synthetic unknown assertion',human_confirmed=True,**selection)
    before=counts(db)
    with pytest.raises(ValueError):approve_reconciliation(db,**{**call,'receipt_id':unknown.id,'reviewed_receipt_digest':unknown.integrity_digest})
    assert counts(db)==before


def test_preview_gate_exemption_is_exact_and_transaction_bounded():
    from app.restore_lock import restore_gate_exempt
    root='/leave/month-review/reconciliation-approval/'+'a'*64
    assert restore_gate_exempt('GET',root)
    assert restore_gate_exempt('POST','/leave/month-review/reconciliation-approve')
    for method,path in [('POST',root),('GET',root+'/x'),('GET',root[:-1]),('GET','/leave/month-review/reconciliation-approve'),('POST','/leave/month-review/reconciliation-approve/x')]:
        assert not restore_gate_exempt(method,path)


@pytest.mark.skipif(os.getenv('MANUAL_APPROVAL_LOCK')!='1',reason='Dedicated committed synthetic DB')
def test_actual_lock_timeout_recovery(history_context):
    import time
    from app.db import SessionLocal
    from sqlalchemy.exc import DBAPIError
    db,ctx,e,selection,receipt,call=fixture(history_context);db.commit()
    with SessionLocal() as holder:
        holder.execute(text('LOCK TABLE leave_reconciliation_approvals IN ACCESS EXCLUSIVE MODE'))
        started=time.monotonic()
        with SessionLocal() as contender:
            with pytest.raises(DBAPIError):approve_reconciliation(contender,**call)
            contender.rollback()
        assert 2.5<time.monotonic()-started<5
    with SessionLocal() as check:
        assert check.scalar(select(func.count()).select_from(LeaveReconciliationApproval))==0
        assert approve_reconciliation(check,**call).id==call['operation_id']
        check.commit()


def test_exact_presentation_trims_only_insignificant_zeroes():
    from app.leave_reconciliation_editor_routes import exact_approval_days
    from decimal import Decimal,localcontext
    for raw,expected in [('7.0000000000000000','7'),('-0.0000000000000000000000000000000000070','-0.000000000000000000000000000000000007'),('2.28571428571428571428571428571428571428571428571429','2.28571428571428571428571428571428571428571428571429')]:
        with localcontext() as context:
            context.prec=2
            shown=exact_approval_days(Decimal(raw))
            assert shown==expected and Decimal(shown)==Decimal(raw)


def test_nonempty_approval_refuses_downgrade(history_context):
    from tests.test_leave_precision_migration import migrate
    from tests.sync_uid_test_support import discard_sync_uid_for_older_downgrade
    db,ctx,e,selection,receipt,call=fixture(history_context)
    row=approve_reconciliation(db,**call);before=counts(db)
    with pytest.raises(RuntimeError,match='history would be lost'):
        discard_sync_uid_for_older_downgrade(db.connection());migrate(db.connection(),'0063_reconciliation_review',down=True)
    assert counts(db)==before and approval_hash(row)==row.integrity_digest
    assert db.scalar(text('SELECT version_num FROM alembic_version'))=='0064_reconciliation_approval'


def test_additive_migration_preserves_populated_receipt_and_legacy(history_context):
    from tests.test_leave_precision_migration import migrate
    from tests.sync_uid_test_support import discard_sync_uid_for_older_downgrade
    db,ctx,e,selection,receipt,call=fixture(history_context)
    receipt_before=db.execute(text('SELECT to_jsonb(t) FROM leave_reconciliation_review_receipts t WHERE id=:id'),{'id':receipt.id}).scalar_one()
    employees_before=db.execute(text('SELECT to_jsonb(t)-\'sync_uid\' FROM employees t WHERE id=:id'),{'id':e.id}).scalar_one()
    ledger_before=db.execute(text('SELECT to_jsonb(t) FROM leave_balance_transactions t ORDER BY id')).scalars().all()
    discard_sync_uid_for_older_downgrade(db.connection());migrate(db.connection(),'0063_reconciliation_review',down=True)
    migrate(db.connection(),'0064_reconciliation_approval')
    migrate(db.connection(),'head')  # 0067 re-adds sync_uid, which the ORM models expect
    assert db.execute(text('SELECT to_jsonb(t) FROM leave_reconciliation_review_receipts t WHERE id=:id'),{'id':receipt.id}).scalar_one()==receipt_before
    assert db.execute(text('SELECT to_jsonb(t)-\'sync_uid\' FROM employees t WHERE id=:id'),{'id':e.id}).scalar_one()==employees_before
    assert db.execute(text('SELECT to_jsonb(t) FROM leave_balance_transactions t ORDER BY id')).scalars().all()==ledger_before
    assert db.scalar(select(func.count()).select_from(LeaveReconciliationApproval))==0
    assert approve_reconciliation(db,**call).id==call['operation_id']
