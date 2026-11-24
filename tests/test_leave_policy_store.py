"""Synthetic real-PG explicit policy persistence, no balance effects."""
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.leave_policy_store import OWNER_DEFAULT_POLICY, persist_policy_version, read_policy_version
from app.models import AuditLog, LeaveBalanceTransaction, LeavePolicyVersion
from tests.test_department_workbook_store import context  # noqa: F401


def test_explicit_policy_version_retry_and_no_ledger(context):
    db, account, _session, auth, _row, _payload = context
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    row=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic initialization',**auth)
    assert read_policy_version(db,policy_id=row.id,**auth)==OWNER_DEFAULT_POLICY
    assert persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic retry',**auth).id==row.id
    changed=persist_policy_version(db,policy=replace(OWNER_DEFAULT_POLICY,annual_deduct_sick=True),reason='Synthetic change',**auth)
    assert changed.id!=row.id
    assert read_policy_version(db,policy_id=row.id,**auth).annual_deduct_sick is False
    assert db.scalar(select(func.count()).select_from(LeavePolicyVersion))==2
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='leave_policy_version_saved'))==2
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before
    account.role='finance';db.flush()
    with pytest.raises(ValueError):
        persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic refusal',**auth)


def test_policy_requires_explicit_reference_integrity_epoch(context):
    db,_account,_session,auth,_row,_payload=context
    with pytest.raises(ValueError):
        read_policy_version(db,policy_id='missing',**auth)
    row=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic',**auth)
    original=row.policy_json
    row.policy_json=original+' ';db.flush()
    with pytest.raises(ValueError,match='integrity'):
        read_policy_version(db,policy_id=row.id,**auth)
    row.policy_json=original;row.restore_epoch='synthetic-stale';db.flush()
    with pytest.raises(ValueError,match='epoch'):
        read_policy_version(db,policy_id=row.id,**auth)


@pytest.mark.parametrize('malformed',[{},[],{'sick_alert_thresholds':None}])
def test_selfhashed_malformed_policy_refuses_controlled(context,malformed):
    import json
    from hashlib import sha256
    db,_account,_session,auth,_row,_payload=context
    row=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic',**auth)
    row.policy_json=json.dumps(malformed);row.policy_sha256=sha256(row.policy_json.encode()).hexdigest();db.flush()
    with pytest.raises(ValueError): read_policy_version(db,policy_id=row.id,**auth)
