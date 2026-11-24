from dataclasses import asdict
import pytest
from sqlalchemy import select,func
from app.leave_policy_edit import parse_policy_fields,prepare_policy_edit,read_policy_edit,confirm_policy_edit
from app.leave_policy_store import OWNER_DEFAULT_POLICY,persist_policy_version
from app.models import LeavePolicyVersion,LeaveBalanceTransaction,LeaveOpeningSelection,LeaveMonthClose
from tests.test_department_workbook_store import context  # noqa


def fields():
    result=asdict(OWNER_DEFAULT_POLICY)
    return {k:('true' if v else 'false') if type(v) is bool else ','.join(map(str,v)) if type(v) is tuple else v for k,v in result.items()}


def test_preview_then_explicit_version_save_no_rebind_or_ledger(context):
    db,_account,_session,auth,_row,_payload=context
    base=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic base',**auth)
    before=[db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,LeaveOpeningSelection,LeaveMonthClose)]
    changed={**fields(),'annual_deduct_sick':'true'}
    draft=prepare_policy_edit(db,base_policy_id=base.id,fields=changed,reason='Synthetic policy preview',**auth)
    assert db.scalar(select(func.count()).select_from(LeavePolicyVersion))==1
    stored,old,new=read_policy_edit(db,draft_id=draft.id,**auth)
    assert not old.annual_deduct_sick and new.annual_deduct_sick
    with pytest.raises(ValueError):confirm_policy_edit(db,draft_id=draft.id,reviewed_sha256=draft.review_sha256,human_confirmed=False,**auth)
    with pytest.raises(ValueError):confirm_policy_edit(db,draft_id=draft.id,reviewed_sha256='0'*64,human_confirmed=True,**auth)
    saved=confirm_policy_edit(db,draft_id=draft.id,reviewed_sha256=draft.review_sha256,human_confirmed=True,**auth)
    assert saved!=base.id
    assert confirm_policy_edit(db,draft_id=draft.id,reviewed_sha256=draft.review_sha256,human_confirmed=True,**auth)==saved
    assert [db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,LeaveOpeningSelection,LeaveMonthClose)]==before


@pytest.mark.parametrize('change',[{'annual_deduct_sick':True},{'annual_year_basis':'hire_anniversary'},{'cash_basis':'guess'},{'sick_alert_thresholds':'90,90'},{'sick_alert_thresholds':'0'},{'sick_alert_thresholds':'1_0'},{'sick_alert_thresholds':'9'*500},{'sick_alert_thresholds':'90, 180'}])
def test_exact_complete_policy_validation(change):
    with pytest.raises(ValueError):parse_policy_fields({**fields(),**change})


def test_draft_tamper_role_epoch_session_refuse(context):
    db,actor,_session,auth,_row,_payload=context
    base=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic',**auth)
    draft=prepare_policy_edit(db,base_policy_id=base.id,fields=fields(),reason='Synthetic',**auth)
    draft.reason+='modified';db.flush()
    with pytest.raises(ValueError):read_policy_edit(db,draft_id=draft.id,**auth)
    draft.reason='Synthetic';db.flush()
    for altered in ({'expected_epoch':'stale'},{'session_version':auth['session_version']+1}):
        with pytest.raises(ValueError):read_policy_edit(db,draft_id=draft.id,**{**auth,**altered})
    actor.role='finance';db.flush()
    with pytest.raises(ValueError):confirm_policy_edit(db,draft_id=draft.id,reviewed_sha256=draft.review_sha256,human_confirmed=True,**auth)


@pytest.mark.parametrize('attribute,value',[('policy_json','{}'),('base_policy_id','0'*64),('actor_id',999999),('restore_epoch','old'),('saved_policy_id','0'*64)])
def test_exact_draft_source_tamper_refuses_without_new_version_or_audit(context,attribute,value):
    from app.models import AuditLog
    db,actor,_session,auth,_row,_payload=context
    base=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic',**auth)
    draft=prepare_policy_edit(db,base_policy_id=base.id,fields=fields(),reason='Synthetic',**auth)
    before=[db.scalar(select(func.count()).select_from(m)) for m in (LeavePolicyVersion,AuditLog)]
    from dataclasses import replace
    if attribute=='actor_id':
        from app.models import AdminAccount
        other=AdminAccount(username='synthetic-other-policy',password_hash='synthetic',role='system_admin',active=True)
        db.add(other);db.flush();value=other.id
    elif attribute in ('base_policy_id','saved_policy_id'):
        other=persist_policy_version(db,policy=replace(OWNER_DEFAULT_POLICY,annual_deduct_sick=True),reason='Synthetic other',**auth)
        value=other.id
        before=[db.scalar(select(func.count()).select_from(m)) for m in (LeavePolicyVersion,AuditLog)]
    setattr(draft,attribute,value);db.flush()
    with pytest.raises(ValueError):confirm_policy_edit(db,draft_id=draft.id,reviewed_sha256=draft.review_sha256,human_confirmed=True,**auth)
    assert [db.scalar(select(func.count()).select_from(m)) for m in (LeavePolicyVersion,AuditLog)]==before


@pytest.mark.parametrize('change',['epoch','session','disabled','reset'])
def test_confirm_rechecks_current_write_authority(context,change):
    from app.models import AuditLog
    db,actor,_session,auth,_row,_payload=context
    base=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic',**auth)
    draft=prepare_policy_edit(db,base_policy_id=base.id,fields=fields(),reason='Synthetic',**auth)
    before=db.scalar(select(func.count()).select_from(AuditLog))
    altered=dict(auth)
    if change=='epoch':altered['expected_epoch']='stale'
    elif change=='session':altered['session_version']+=1
    elif change=='disabled':actor.active=False
    else:actor.pending_credential_reset=True
    db.flush()
    with pytest.raises(ValueError):confirm_policy_edit(db,draft_id=draft.id,reviewed_sha256=draft.review_sha256,human_confirmed=True,**altered)
    assert db.scalar(select(func.count()).select_from(AuditLog))==before


def test_identical_drafts_reuse_exact_version_and_exclusive_guard(context):
    from app.db import SessionLocal
    from sqlalchemy import text
    from app.restore_lock import RESTORE_LOCK_KEY
    db,_actor,_session,auth,_row,_payload=context
    base=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic base',**auth)
    drafts=[prepare_policy_edit(db,base_policy_id=base.id,fields=fields(),reason='Synthetic',**auth) for _ in range(2)]
    # Independent real PGsession cannot acquire same transaction lock. All service
    # writers take this lock before policy lookup or inserts, including differentdrafts.
    with SessionLocal() as other:
        assert other.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),{'key':RESTORE_LOCK_KEY}) is False
    for draft in drafts:
        assert confirm_policy_edit(db,draft_id=draft.id,reviewed_sha256=draft.review_sha256,human_confirmed=True,**auth)==base.id
    assert db.scalar(select(func.count()).select_from(LeavePolicyVersion))==1
