"""Durable exact preparation selection, no accepted balances."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.leave_manual_opening_groups import persist_manual_opening_group, read_manual_opening_group
from app.models import AuditLog, LeaveBalanceTransaction, LeaveManualOpeningGroup
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401


def args(fact, auth):
    return dict(employee_id=fact.employee_id, balances={'A':Decimal('1.00'),'W':Decimal(2),'H':Decimal(0)},
        effective_start=date(2026,5,17), reason='Synthetic durable selection', **auth)


def test_durable_exact_group_reopen_retry_and_conflicting_group(history_context):  # noqa: F811
    db, _account, auth, fact = history_context
    count = db.scalar(select(func.count()).select_from(LeaveManualOpeningGroup))
    ledger = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    first, reviews = persist_manual_opening_group(db, **args(fact,auth))
    assert tuple(read_manual_opening_group(db, group_id=first.id, **auth)) == reviews
    same, _ = persist_manual_opening_group(db, **args(fact,auth))
    assert first.id == same.id
    other_args = args(fact,auth); other_args['balances']['A'] = Decimal(4)
    other, _ = persist_manual_opening_group(db, **other_args)
    assert first.id != other.id
    assert read_manual_opening_group(db, group_id=first.id, **auth) == reviews
    assert db.scalar(select(func.count()).select_from(LeaveManualOpeningGroup)) == count+2
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == ledger
    assert not any(r['applied'] for r in reviews)


@pytest.mark.parametrize('change',['role','session','version','reset','epoch','binding','missing'])
def test_saved_group_rechecks_scope_epoch_and_exact_binding(history_context, change):  # noqa: F811
    db, account, auth, fact = history_context
    group, _reviews = persist_manual_opening_group(db, **args(fact,auth))
    if change == 'role': account.role='finance'
    if change == 'version': account.session_version += 1
    if change == 'reset': account.pending_credential_reset=True
    if change == 'session': auth = {**auth,'session_id':'missing-synthetic'}
    if change == 'epoch': group.restore_epoch='old-synthetic'
    if change == 'binding': group.weekly_id=group.annual_id
    if change == 'missing': auth={**auth}; group.id='new-synthetic-group-id'
    db.flush()
    with pytest.raises(ValueError):
        read_manual_opening_group(db, group_id=group.id if change!='missing' else 'absent', **auth)


def test_invalid_late_group_rolls_back_group_proposals_and_audit(history_context):  # noqa: F811
    from app.models import LeaveHistoricalEvidencePreparation
    db, _account, auth, fact = history_context
    models = (LeaveManualOpeningGroup, LeaveHistoricalEvidencePreparation, AuditLog)
    before = [db.scalar(select(func.count()).select_from(m)) for m in models]
    values = args(fact,auth); values['balances']['H']=Decimal(-1)
    with pytest.raises(ValueError): persist_manual_opening_group(db, **values)
    assert [db.scalar(select(func.count()).select_from(m)) for m in models] == before


@pytest.mark.parametrize('state',[{'applied':True},{'requires_human_review':False},
    {'superseded':True},{'status':'approved'}])
def test_group_cannot_reopen_nonpending_source_flags(history_context,state):  # noqa: F811
    import json
    from hashlib import sha256

    from app.models import LeaveHistoricalEvidencePreparation
    db,_account,auth,fact=history_context
    group,_=persist_manual_opening_group(db,**args(fact,auth))
    row=db.get(LeaveHistoricalEvidencePreparation,group.annual_id)
    proposal=json.loads(row.proposal_json);proposal.update(state)
    row.proposal_json=json.dumps(proposal);row.proposal_sha256=sha256(row.proposal_json.encode()).hexdigest();db.flush()
    with pytest.raises(ValueError,match='Pending'):
        read_manual_opening_group(db,group_id=group.id,**auth)
