"""Explicit confirmed manual source history. Never posts/rebases host ledger."""
import json
import secrets
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_manual_opening_groups import read_manual_opening_group
from app.leave_policy_store import canonical_policy, policy_read_guard, read_policy_version
from app.models import AuditLog, LeaveOpeningSelection


def _source(db, *, group_id, policy_id, **identity):
    reviews=read_manual_opening_group(db,group_id=group_id,**identity)
    policy=read_policy_version(db,policy_id=policy_id,**identity)
    digest=sha256(json.dumps({'group_id':group_id,'policy_id':policy_id,
        'policy':canonical_policy(policy),'proposals':[(r['proposal_id'],r['proposal_sha256']) for r in reviews]},
        sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return reviews,policy,digest


def _current(db, employee_id, epoch):
    return db.scalar(select(LeaveOpeningSelection).where(
        LeaveOpeningSelection.employee_id==employee_id,
        LeaveOpeningSelection.restore_epoch.is_(None) if epoch is None else LeaveOpeningSelection.restore_epoch==epoch)
        .order_by(LeaveOpeningSelection.revision.desc()).limit(1).execution_options(populate_existing=True))


@bounded_workbook_operation
def confirm_opening_source(db, *, group_id, policy_id, expected_previous_id,
                           reviewed_source_sha256, reason, human_confirmed, **identity):
    """CAS replacement must name prior selection. Exact retries are idempotent.

    Confirms only entered opening source for subsequent calculation review.
    Existing ledger is not deemed equivalent, reconciled, replaced or posted.
    Caller owns transaction. Fresh locked policy/group are re-read on retry.
    """
    account=_guard(db,**identity)
    if human_confirmed is not True or not isinstance(reason,str) or not reason.strip() or len(reason)>1000:
        raise ValueError('Explicit human confirmation and bounded reason required.')
    reviews,_policy,digest=_source(db,group_id=group_id,policy_id=policy_id,**identity)
    if digest!=reviewed_source_sha256:
        raise ValueError('Reviewed source changed.')
    employee_id=reviews[0]['employee_id']
    current=_current(db,employee_id,identity['expected_epoch'])
    if current is not None and current.group_id==group_id and current.policy_id==policy_id:
        if current.source_sha256!=digest:
            raise ValueError('Stored source integrity mismatch.')
        return current
    if (current.id if current else None)!=expected_previous_id:
        raise ValueError('Opening source selection changed; review current source first.')
    # Revision remains globally monotonic across restore epochs in this DB.
    previous=db.scalar(select(LeaveOpeningSelection).where(LeaveOpeningSelection.employee_id==employee_id)
        .order_by(LeaveOpeningSelection.revision.desc()).limit(1))
    row=LeaveOpeningSelection(id=secrets.token_hex(32),employee_id=employee_id,
        group_id=group_id,policy_id=policy_id,previous_id=current.id if current else None,
        revision=previous.revision+1 if previous else 1,actor_id=account.id,
        source_sha256=digest,reason=reason.strip(),restore_epoch=identity['expected_epoch'],created_at=datetime.now(UTC))
    db.add(row);db.flush()
    db.add(AuditLog(actor=account.username,action='leave_opening_source_confirmed',
        entity_type='leave_opening_selection',entity_id=row.id,
        old_value=current.id if current else None,
        new_value=json.dumps({'group_id':group_id,'policy_id':policy_id,'source_sha256':digest,'ledger_applied':False}),
        reason=reason.strip()))
    db.flush()
    return row


@bounded_workbook_operation
def review_opening_source(db, *, group_id, policy_id, **identity):
    policy_read_guard(db,**identity)
    reviews,policy,digest=_source(db,group_id=group_id,policy_id=policy_id,**identity)
    current=_current(db,reviews[0]['employee_id'],identity['expected_epoch'])
    return {'reviews':reviews,'policy':policy,'source_sha256':digest,
        'expected_previous_id':current.id if current else None,'ledger_applied':False,
        'is_current':bool(current and current.group_id==group_id and current.policy_id==policy_id and current.source_sha256==digest)}


@bounded_workbook_operation
def read_confirmed_opening_source(db, *, selection_id, **identity):
    policy_read_guard(db,**identity)
    row=db.get(LeaveOpeningSelection,selection_id,populate_existing=True)
    if row is None or row.restore_epoch!=identity['expected_epoch']:
        raise ValueError('Confirmed current-epoch source required.')
    current=_current(db,row.employee_id,identity['expected_epoch'])
    if current.id!=row.id:
        raise ValueError('Source replaced; explicitly review the current selection.')
    reviews,policy,digest=_source(db,group_id=row.group_id,policy_id=row.policy_id,**identity)
    if digest!=row.source_sha256 or reviews[0]['employee_id']!=row.employee_id:
        raise ValueError('Confirmed source integrity mismatch.')
    return reviews,policy,digest
