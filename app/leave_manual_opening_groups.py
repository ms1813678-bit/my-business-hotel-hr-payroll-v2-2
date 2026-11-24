"""Persisted exact manual preparation selection, no approve/apply behavior."""
import json
import secrets
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_manual_opening import prepare_manual_opening_group, review_manual_opening
from app.models import (
    AdminAccount,
    AdminSession,
    AuditLog,
    LeaveHistoricalEvidencePreparation,
    LeaveManualOpeningGroup,
)
from app.restore_lock import acquire_authenticated_read_lock, read_restore_epoch


def _selection(reviews):
    return sha256(json.dumps([(r['code'], r['proposal_id'], r['proposal_sha256'])
        for r in reviews], separators=(',', ':')).encode()).hexdigest()


@bounded_workbook_operation
def persist_manual_opening_group(db, **values):
    """Existing prepare guard holds exclusive lock before proposals or group writes."""
    with db.begin_nested():
        reviews = prepare_manual_opening_group(db, **values)
        digest = _selection(reviews)
        row = db.scalar(select(LeaveManualOpeningGroup).where(LeaveManualOpeningGroup.selection_sha256==digest))
        if row is not None:
            if row.restore_epoch != read_restore_epoch(db):
                raise ValueError('Manual group belongs to previous restore.')
            return row, reviews
        by_code = {r['code']:r['proposal_id'] for r in reviews}
        row = LeaveManualOpeningGroup(id=secrets.token_hex(32), actor_id=values['actor_id'],
            annual_id=by_code['A'], weekly_id=by_code['W'], holiday_id=by_code['H'],
            selection_sha256=digest, restore_epoch=read_restore_epoch(db), created_at=datetime.now(UTC))
        db.add(row); db.flush()
        db.add(AuditLog(actor=db.get(AdminAccount, values['actor_id']).username, action='manual_opening_group_prepared',
            entity_type='leave_manual_opening_group', entity_id=row.id,
            new_value=json.dumps({'selection_sha256':digest,'applied':False}),
            reason='Exact three-category selection, no approval or ledger effect'))
        db.flush()
        return row, reviews


@bounded_workbook_operation
def read_manual_opening_group(db, *, group_id, actor_id, session_id, session_version, expected_epoch):
    """Read lock only, compatible with GET middleware; rechecks current HR scope."""
    acquire_authenticated_read_lock(db)
    if read_restore_epoch(db) != expected_epoch:
        raise ValueError('Manual group restore epoch changed.')
    actor = db.scalar(select(AdminAccount).where(AdminAccount.id==actor_id).execution_options(populate_existing=True))
    login = db.scalar(select(AdminSession).where(AdminSession.id==session_id).execution_options(populate_existing=True))
    if (actor is None or login is None or login.admin_account_id!=actor.id or not actor.active
            or actor.must_change_password or actor.pending_credential_reset or actor.role!='system_admin'
            or actor.session_version!=session_version):
        raise ValueError('Current HR session required.')
    group = db.scalar(select(LeaveManualOpeningGroup).where(LeaveManualOpeningGroup.id==group_id).execution_options(populate_existing=True))
    if group is None or group.restore_epoch!=expected_epoch:
        raise ValueError('Manual group unavailable.')
    rows = [db.get(LeaveHistoricalEvidencePreparation, i, populate_existing=True)
        for i in (group.annual_id, group.weekly_id, group.holiday_id)]
    if any(row is None or row.restore_epoch!=expected_epoch for row in rows):
        raise ValueError('Manual group source unavailable.')
    reviews = tuple(review_manual_opening(row) for row in rows)
    if ([r['code'] for r in reviews]!=['A','W','H'] or _selection(reviews)!=group.selection_sha256
            or len({(r['employee_id'],r['effective_start'],r['reason']) for r in reviews})!=1):
        raise ValueError('Manual group source binding mismatch.')
    return reviews
