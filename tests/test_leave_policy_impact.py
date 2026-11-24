"""Actual migrated PostgreSQL earned-impact evidence, synthetic rolled back."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import select,func,inspect
from app.leave_policy_impact import preview_earned_policy_impact
from app.leave_policy_edit import prepare_policy_edit
from app.leave_policy_store import persist_policy_version,OWNER_DEFAULT_POLICY
from app.models import LeaveOpeningSelection,LeaveRegisterEntry,LeaveBalanceTransaction,AuditLog,LeavePolicyVersion,LeaveMonthClose
from tests.test_leave_source_reconciliation import setup_source
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_policy_edit import fields


def setup_impact(history_context):
    db,e,args=setup_source(history_context)
    day=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,26)))
    day.code='S';db.flush()
    selection=db.get(LeaveOpeningSelection,args['selection_ids'][0])
    auth={k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    # Must use exact currentselectedbase, not convenience defaults.
    from app.leave_policy_store import read_policy_version
    from dataclasses import asdict
    base=read_policy_version(db,policy_id=selection.policy_id,**auth)
    f=asdict(replace(base,annual_deduct_sick=True))
    f={k:('true' if v else 'false') if type(v)is bool else ','.join(map(str,v)) if type(v)is tuple else v for k,v in f.items()}
    draft=prepare_policy_edit(db,base_policy_id=selection.policy_id,fields=f,reason='Synthetic impact',**auth)
    return db,e,dict(draft_id=draft.id,reviewed_sha256=draft.review_sha256,**args)


def test_actual_sick_delta_same_source_and_zero_side_effects(history_context):
    db,e,args=setup_impact(history_context)
    models=(LeaveOpeningSelection,LeaveRegisterEntry,LeaveBalanceTransaction,AuditLog,LeavePolicyVersion,LeaveMonthClose)
    def snapshot():
        return [tuple(tuple(getattr(row,c.key) for c in inspect(m).columns) for row in db.scalars(select(m))) for m in models]
    before=snapshot()
    result=preview_earned_policy_impact(db,**args)
    annual=next(r for r in result.rows if r.code=='A')
    assert annual.before_earned>annual.after_earned
    assert abs(annual.earned_delta+Decimal(15)/Decimal(12)/Decimal(28))<Decimal('1e-27')
    assert next(r for r in result.rows if r.code=='W').earned_delta==0
    assert not result.posting_allowed and result.through==date(2026,2,28)
    assert snapshot()==before
    assert preview_earned_policy_impact(db,**args)==result
    db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,26))).code=''
    db.flush()
    changed=preview_earned_policy_impact(db,**args)
    assert changed.source_digest!=result.source_digest
    assert next(r for r in changed.rows if r.code=='A').earned_delta==0


def test_unknown_target_and_wrong_review_and_wrong_base_refuse(history_context):
    db,e,args=setup_impact(history_context)
    with pytest.raises(ValueError):preview_earned_policy_impact(db,**{**args,'reviewed_sha256':'0'*64})
    day=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,26)))
    day.code=None;db.flush()
    with pytest.raises(ValueError,match='Unknown'):preview_earned_policy_impact(db,**args)
    day.code='';db.flush()
    auth={k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    other=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic other base',**auth)
    draft=prepare_policy_edit(db,base_policy_id=other.id,fields=fields(),reason='Synthetic wrongbase',**auth)
    with pytest.raises(ValueError,match='baseline'):preview_earned_policy_impact(db,**{**args,'draft_id':draft.id,'reviewed_sha256':draft.review_sha256})


@pytest.mark.parametrize('change',['epoch','session','finance','disabled','otheractor'])
def test_impact_rechecks_authority_no_writes(history_context,change):
    db,e,args=setup_impact(history_context)
    auth={k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    from app.models import AdminAccount,AdminSession
    actor=db.get(AdminAccount,args['actor_id'])
    if change=='epoch':args['expected_epoch']='stale'
    elif change=='session':args['session_version']+=1
    elif change=='finance':actor.role='finance'
    elif change=='disabled':actor.active=False
    else:
        other=AdminAccount(username='synthetic-impact-other',role='system_admin',active=True,password_hash='synthetic')
        db.add(other);db.flush()
        session=AdminSession(id='synthetic-impact-other-session',admin_account_id=other.id)
        db.add(session);db.flush()
        args.update(actor_id=other.id,session_id=session.id,session_version=other.session_version)
    db.flush()
    before=[db.scalar(select(func.count()).select_from(m)) for m in (AuditLog,LeaveBalanceTransaction)]
    with pytest.raises(ValueError):preview_earned_policy_impact(db,**args)
    assert [db.scalar(select(func.count()).select_from(m)) for m in (AuditLog,LeaveBalanceTransaction)]==before


def test_explicit_inputs_bind_even_equal_earned_values_and_closed_rows_unchanged(history_context):
    from datetime import datetime,UTC
    from app.models import BackupRun
    db,e,args=setup_impact(history_context)
    backup=BackupRun(backup_type='manual',status='success',size_bytes=0,initiated_by='synthetic')
    db.add(backup);db.flush()
    close=LeaveMonthClose(year=2026,month=2,backup_run_id=backup.id,closed_by='synthetic',closed_at=datetime.now(UTC),policy_snapshot_json='synthetic closed source')
    db.add(close);db.flush()
    snapshot=tuple(getattr(close,c.key) for c in inspect(LeaveMonthClose).columns)
    a=preview_earned_policy_impact(db,**args)
    changed={**args,'age_evaluation_dates':{**args['age_evaluation_dates'],date(2026,2,1):date(2026,2,2)}}
    b=preview_earned_policy_impact(db,**changed)
    assert a.rows==b.rows and a.source_digest!=b.source_digest
    assert a.closed_months==((close.id,2026,2,'synthetic closed source'),)
    db.refresh(close)
    assert tuple(getattr(close,c.key) for c in inspect(LeaveMonthClose).columns)==snapshot


def test_source_mismatch_refuses_even_when_projection_values_equal(history_context,monkeypatch):
    import app.leave_policy_impact as module
    from dataclasses import replace
    db,e,args=setup_impact(history_context)
    original=module._manual_department_projection
    def changed(*a,**kw):
        projection,department=original(*a,**kw)
        return replace(projection,policy_independent_source_sha256='0'*64),department
    monkeypatch.setattr(module,'_manual_department_projection',changed)
    with pytest.raises(ValueError,match='policy-independent'):preview_earned_policy_impact(db,**args)


def test_dirty_caller_state_refuses_before_autoflush(history_context):
    db,e,args=setup_impact(history_context)
    e.full_name='Synthetic dirty unsaved'
    with pytest.raises(ValueError,match='Clean read-only'):preview_earned_policy_impact(db,**args)


def test_tiny_negative_and_positive_delta_stay_visible_in_actual_template():
    from app.main import templates
    from starlette.requests import Request
    from types import SimpleNamespace as S
    request=Request({'type':'http','method':'GET','path':'/leave/policies/impact','headers':[],'scheme':'http','server':('localhost',80),'root_path':'','query_string':b''})
    request.state.admin_role='system_admin'
    result=S(rows=[S(employee_id=1,code='A',before_earned=Decimal('0.004'),after_earned=Decimal('0.003'),earned_delta=Decimal('-0.001')),S(employee_id=1,code='H',before_earned=Decimal('0.003'),after_earned=Decimal('0.004'),earned_delta=Decimal('0.001'))],through=date(2026,2,28),source_digest='a'*64,closed_months=())
    html=templates.get_template('leave_policy_impact.html').render(request=request,admin='Synthetic',csrf_token='synthetic',draft=S(id='b'*64),departments=[],department_id=None,month='',months=[],choices=[],result=result,result_employees={1:S(code='SYN',full_name='Synthetic')},submitted={})
    assert html.count('تغير غير صفري أقل من 0.01 يوم؛ القيمة الدقيقة أدناه.')==2
    assert '| -0.001' in html and '| 0.001' in html
