"""Actual migrated PostgreSQL, explicit synthetic attestations only."""
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
import json

import pytest
from sqlalchemy import select, func, event

from app.models import LeaveBalanceTransaction, LeaveSyntheticTransition, LeaveSyntheticTransitionEntry, AuditLog, LeaveType, LeaveRegisterEntry
from app.config import settings
from app.leave_source_reconciliation import reconcile_selected_opening
from app.leave_transition_draft import draft_transition, LedgerDecision, OpeningReleaseDecision
from app.leave_synthetic_transition import apply_synthetic_transition
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_source_reconciliation import setup_source


def prepare(history_context, monkeypatch, ledger=False):
    monkeypatch.setattr(settings, 'leave_synthetic_transition_enabled', True)
    monkeypatch.setattr(settings, 'app_env', 'test')
    db,e,args=setup_source(history_context)
    # Explicit synthetic fixture assumption, never alter actual host settings.
    for kind in db.scalars(select(LeaveType).where(LeaveType.code.in_(('annual','weekly_rest','official_holiday')))):
        kind.tracks_balance=True
    db.flush()
    if ledger:
        kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
        db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=kind.id,transaction_type='adjustment',days_delta=Decimal('-0.0000000000000000000000000000000000007'),reason='synthetic external',created_by='synthetic'))
        db.flush()
    args['release_evaluated_on']=date(2026,2,28)
    source=reconcile_selected_opening(db,**args)
    ld=tuple(LedgerDecision(i,'external_to_scenario','synthetic fixture explicit external assertion') for r in source.rows for i in r.ledger_row_ids)
    od=tuple(OpeningReleaseDecision(r.employee_id,r.code,True,'synthetic fixture explicit opening release assertion') for r in source.rows)
    review=dict(reviewed_reconciliation_digest=source.reconciliation_digest,ledger_decisions=ld,opening_decisions=od)
    draft=draft_transition(db,**review,**args)
    call=dict(operation_id='a'*64,reviewed_decision_digest=draft.decision_digest,reason='Synthetic only transition',human_confirmed=True,**review,**args)
    return db,e,source,draft,review,args,call


def counts(db):
    return tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,LeaveSyntheticTransition,AuditLog))


def test_draft_exact_external_and_included_coverage_and_no_writes(history_context,monkeypatch):
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch,True)
    before=counts(db)
    annual=next(r for r in draft.rows if r.code=='A')
    with localcontext() as ctx:
        ctx.prec=100
        assert annual.reviewed_target==Decimal('7.2500000000000001')-Decimal('0.0000000000000000000000000000000000007')
    assert not draft.posting_allowed and not draft.establishes_spendable_credit
    assert annual.adjustment_delta==Decimal('7.2500000000000001')
    included=draft_transition(db,**{**review,'ledger_decisions':tuple(replace(d,treatment='included_in_scenario') for d in review['ledger_decisions'])},**args)
    assert next(r for r in included.rows if r.code=='A').reviewed_target==Decimal('7.2500000000000001')
    assert counts(db)==before
    for decisions in ((),review['ledger_decisions']*2,(LedgerDecision(2147483647,'unresolved','synthetic'),)):
        with pytest.raises(ValueError):draft_transition(db,**{**review,'ledger_decisions':decisions},**args)
    with pytest.raises(ValueError):draft_transition(db,**{**review,'opening_decisions':review['opening_decisions'][:-1]},**args)


@pytest.mark.parametrize('field', ['opening','ledger'])
def test_unresolved_refuses_atomic_commit(history_context,monkeypatch,field):
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch,True)
    if field=='opening':review['opening_decisions']=tuple(replace(d,released=None) for d in review['opening_decisions'])
    else:review['ledger_decisions']=tuple(replace(d,treatment='unresolved') for d in review['ledger_decisions'])
    draft=draft_transition(db,**review,**args)
    assert any(r.unresolved and r.adjustment_delta is None for r in draft.rows)
    before=counts(db)
    with pytest.raises(ValueError,match='Unresolved'):apply_synthetic_transition(db,**{**call,**review,'reviewed_decision_digest':draft.decision_digest})
    assert counts(db)==before


def test_append_only_delta_replay_and_second_key_refusal(history_context,monkeypatch):
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch,True)
    old=[(r.id,r.days_delta) for r in db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id==e.id))]
    before=counts(db)
    row=apply_synthetic_transition(db,**call)
    result=json.loads(row.result_json)
    assert len(result)==3 and any(r['ledger_id'] is None and r['delta']=='0' for r in result)
    assert all(db.get(LeaveBalanceTransaction,i).days_delta==v for i,v in old)
    after=counts(db)
    assert after[1]==before[1]+1 and after[2]==before[2]+1
    assert apply_synthetic_transition(db,**call).id==row.id and counts(db)==after
    with pytest.raises(ValueError,match='scope already'):apply_synthetic_transition(db,**{**call,'operation_id':'b'*64})
    with pytest.raises(ValueError,match='request'):apply_synthetic_transition(db,**{**call,'reason':'changed'})
    # Source changed after success: replay fails, not a stale success response.
    day=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id))
    day.code='W';db.flush()
    with pytest.raises(ValueError):apply_synthetic_transition(db,**call)
    assert counts(db)==after


@pytest.mark.parametrize('boundary',['ledger','operation','entry','audit'])
def test_injected_failure_rolls_back_every_local_boundary(history_context,monkeypatch,boundary):
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch)
    before=counts(db)
    def fail(session,ctx,objects):
        for obj in session.new:
            if ((boundary=='ledger' and isinstance(obj,LeaveBalanceTransaction))
                    or (boundary=='operation' and isinstance(obj,LeaveSyntheticTransition))
                    or (boundary=='entry' and isinstance(obj,LeaveSyntheticTransitionEntry))
                    or (boundary=='audit' and isinstance(obj,AuditLog) and obj.action=='synthetic_leave_transition')):
                raise RuntimeError('synthetic injected boundary failure')
    event.listen(db,'before_flush',fail)
    try:
        with pytest.raises(RuntimeError):apply_synthetic_transition(db,**call)
    finally:event.remove(db,'before_flush',fail)
    assert counts(db)==before
    assert apply_synthetic_transition(db,**call).id=='a'*64


@pytest.mark.parametrize('change',['disabled','environment','stale','confirmation','decision','epoch','role'])
def test_gates_freshness_and_authority(history_context,monkeypatch,change):
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch)
    before=counts(db)
    if change=='disabled':monkeypatch.setattr(settings,'leave_synthetic_transition_enabled',False)
    elif change=='environment':monkeypatch.setattr(settings,'app_env','production')
    elif change=='stale':call['reviewed_reconciliation_digest']='c'*64
    elif change=='confirmation':call['human_confirmed']=False
    elif change=='decision':call['reviewed_decision_digest']='c'*64
    elif change=='epoch':call['expected_epoch']='stale'
    elif change=='role':
        from app.models import AdminAccount
        db.get(AdminAccount,args['actor_id']).role='finance';db.flush()
    with pytest.raises(ValueError):apply_synthetic_transition(db,**call)
    assert counts(db)==before


def test_tracking_change_changes_digest_and_blocks_draft(history_context,monkeypatch):
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch)
    kind=db.scalar(select(LeaveType).where(LeaveType.code=='official_holiday'))
    kind.tracks_balance=False;db.flush()
    with pytest.raises(ValueError,match='changed'):draft_transition(db,**review,**args)
    source=reconcile_selected_opening(db,**args)
    current=draft_transition(db,**{**review,'reviewed_reconciliation_digest':source.reconciliation_digest},**args)
    assert 'category_not_balance_tracked' in next(r for r in current.rows if r.code=='H').unresolved


def test_closed_period_and_immutable_operation_entries_and_ledger(history_context,monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from app.models import LeaveMonthClose
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch)
    # Existing close schema fields come from the current host, no invented authority.
    from app.models import BackupRun
    from datetime import datetime,UTC
    backup=BackupRun(backup_type='manual',status='success',size_bytes=0,initiated_by='synthetic');db.add(backup);db.flush()
    close=LeaveMonthClose(year=2026,month=2,backup_run_id=backup.id,closed_at=datetime.now(UTC),policy_snapshot_json='{}',closed_by='synthetic')
    db.add(close);db.flush()
    with pytest.raises(ValueError,match='Closed'):apply_synthetic_transition(db,**call)
    db.delete(close);db.flush()
    op=apply_synthetic_transition(db,**call)
    ledger_id=next(r['ledger_id'] for r in json.loads(op.result_json) if r['ledger_id'])
    for statement,params in (
        ('UPDATE leave_synthetic_transitions SET request_digest=:v WHERE id=:id',{'v':'f'*64,'id':op.id}),
        ('DELETE FROM leave_synthetic_transition_entries WHERE operation_id=:id',{'id':op.id}),
        ('UPDATE leave_balance_transactions SET reason=:v WHERE id=:id',{'v':'changed','id':ledger_id}),
        ('DELETE FROM leave_balance_transactions WHERE id=:id',{'id':ledger_id})):
        with pytest.raises(DBAPIError):
            with db.begin_nested():db.execute(text(statement),params)
    assert apply_synthetic_transition(db,**call).id==op.id


@pytest.mark.skipif(__import__('os').environ.get('MANUAL_LEAVE_TRANSITION_CONCURRENCY')!='1',reason='Standalone dedicated synthetic_leave DB only, commits fixture')
def test_actual_independent_sessions_serialize_replay_and_second_key(history_context,monkeypatch):
    from fastapi import HTTPException
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.db import SessionLocal
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch)
    db.commit()
    barrier=Barrier(2)
    def run(opid):
        with SessionLocal() as other:
            barrier.wait(timeout=5)
            try:
                result=apply_synthetic_transition(other,**{**call,'operation_id':opid})
                id_=result.id
                other.commit()
                return ('success',id_)
            except HTTPException as exc:
                other.rollback()
                assert exc.status_code == 503
                assert exc.detail == 'النظام مشغول مؤقتًا، أعد المحاولة بعد قليل.'
                return ('busy', None)
            except ValueError as exc:
                other.rollback()
                return ('refused',str(exc))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,'a'*64),pool.submit(run,'b'*64)]
        race=[f.result(timeout=15) for f in futures]
    assert sum(r[0]=='success' for r in race)==1
    assert all(r[0] in {'success','refused','busy'} for r in race)
    winner=next(r[1] for r in race if r[0]=='success')
    loser='b'*64 if winner=='a'*64 else 'a'*64
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,winner),pool.submit(run,winner)]
        results=[f.result(timeout=15) for f in futures]
    assert any(r==('success',winner) for r in results)
    assert all(r==('success',winner) or r==('busy',None) for r in results)
    # Busy admission is not a replay receipt. Retry after both holders exit.
    with SessionLocal() as other:
        assert apply_synthetic_transition(other,**{**call,'operation_id':winner}).id==winner
        other.commit()
    # New operation key cannot duplicate a baseline, even in independent sessions.
    with SessionLocal() as other:
        with pytest.raises(ValueError,match='scope already'):apply_synthetic_transition(other,**{**call,'operation_id':loser})
        other.rollback()
        assert other.scalar(select(func.count()).select_from(LeaveSyntheticTransition))==1


def test_same_employee_replaced_selection_cannot_reset_baseline_scope(history_context,monkeypatch):
    from app.leave_manual_opening_groups import persist_manual_opening_group
    from app.leave_opening_source import review_opening_source,confirm_opening_source
    from app.models import LeaveOpeningSelection
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch)
    apply_synthetic_transition(db,**call)
    auth={k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    old=db.get(LeaveOpeningSelection,args['selection_ids'][0])
    group,_=persist_manual_opening_group(db,employee_id=e.id,balances={'A':Decimal(12),'W':Decimal(2),'H':Decimal(0)},effective_start=date(2026,2,25),reason='Synthetic changed source',**auth)
    changed=review_opening_source(db,group_id=group.id,policy_id=old.policy_id,**auth)
    selected=confirm_opening_source(db,group_id=group.id,policy_id=old.policy_id,expected_previous_id=old.id,reviewed_source_sha256=changed['source_sha256'],reason='Synthetic changed source',human_confirmed=True,**auth)
    args['selection_ids']=(selected.id,)
    before=counts(db)
    with pytest.raises(ValueError,match='scope already'):
        apply_synthetic_transition(db,**{**call,**args,'operation_id':'c'*64})
    assert counts(db)==before


def test_low_decimal_context_and_unlinked_legacy_update_preserved(history_context,monkeypatch):
    from sqlalchemy import text
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch,True)
    with localcontext() as ctx:
        ctx.prec=4
        assert draft_transition(db,**review,**args)==draft
    row_id=review['ledger_decisions'][0].ledger_id
    old=db.get(LeaveBalanceTransaction,row_id).days_delta
    db.execute(text('UPDATE leave_balance_transactions SET reason=:r WHERE id=:id'),{'r':'Synthetic updated legacy reason','id':row_id})
    db.expire_all()
    legacy=db.get(LeaveBalanceTransaction,row_id)
    assert legacy.reason=='Synthetic updated legacy reason' and legacy.days_delta==old
    # Provenance edits, even equal amount, invalidate the reviewed source.
    with pytest.raises(ValueError,match='changed'):draft_transition(db,**review,**args)


def test_inactive_category_changes_source_and_blocks_even_refreshed_draft(history_context,monkeypatch):
    db,e,source,draft,review,args,call=prepare(history_context,monkeypatch)
    kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
    kind.active=False;db.flush()
    with pytest.raises(ValueError,match='changed'):draft_transition(db,**review,**args)
    fresh=reconcile_selected_opening(db,**args)
    current=draft_transition(db,**{**review,'reviewed_reconciliation_digest':fresh.reconciliation_digest},**args)
    assert 'category_inactive' in next(r for r in current.rows if r.code=='A').unresolved
