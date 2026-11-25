"""Explicit versioned leave settings. Never recalculates or selects latest."""
import json
import secrets
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_policy_preview import LeavePolicy
from app.models import AdminAccount, AdminSession, AuditLog, LeavePolicyVersion
from app.restore_lock import acquire_authenticated_read_lock, read_restore_epoch

# User defaults Nov23 16:45:27/16:45:42. Calendar confirmed18:35:51.
# A constant is not a silently active stored policy. Initialization is explicit.
OWNER_DEFAULT_POLICY = LeavePolicy(True, True, True, False, 'calendar', 'prorated', False, (90, 180))


def canonical_policy(policy):
    if type(policy) is not LeavePolicy:
        raise TypeError('Explicit complete leave policy required.')
    # Revalidate even objects rebuilt through low-level internal construction.
    policy = LeavePolicy(**asdict(policy))
    return json.dumps(asdict(policy), sort_keys=True, separators=(',', ':'))


def policy_read_guard(db, *, actor_id, session_id, session_version, expected_epoch):
    acquire_authenticated_read_lock(db)
    if read_restore_epoch(db) != expected_epoch:
        raise ValueError('Restore epoch changed.')
    account=db.get(AdminAccount,actor_id,populate_existing=True)
    login=db.get(AdminSession,session_id,populate_existing=True)
    if (account is None or login is None or login.admin_account_id!=account.id
            or not account.active or account.must_change_password or account.pending_credential_reset
            or account.role!='system_admin' or account.session_version!=session_version):
        raise ValueError('Current authorized HR session required.')
    return account


@bounded_workbook_operation
def read_policy_version(db, *, policy_id, **identity):
    policy_read_guard(db, **identity)
    return _read_verified_policy(db,policy_id=policy_id,expected_epoch=identity['expected_epoch'])


def _read_verified_policy(db, *, policy_id, expected_epoch):
    row = db.scalar(select(LeavePolicyVersion).where(LeavePolicyVersion.id == policy_id)
        .execution_options(populate_existing=True))
    if row is None or row.restore_epoch != expected_epoch:
        raise ValueError('Explicit current-epoch policy version required.')
    if sha256(row.policy_json.encode()).hexdigest() != row.policy_sha256:
        raise ValueError('Stored policy integrity mismatch.')
    try:
        values = json.loads(row.policy_json)
        if not isinstance(values,dict):
            raise ValueError('Policy object required.')
        values['sick_alert_thresholds'] = tuple(values['sick_alert_thresholds'])
        policy = LeavePolicy(**values)
        if canonical_policy(policy) != row.policy_json:
            raise ValueError('Canonical complete policy required.')
        return policy
    except (KeyError,TypeError,AttributeError) as exc:
        raise ValueError('Malformed policy source.') from exc


@bounded_workbook_operation
def persist_policy_version(db, *, policy, reason, **identity):
    """Caller commits. An explicit new version does not replace previous use."""
    account = _guard(db, **identity)
    if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:
        raise ValueError('Explicit bounded policy review reason required.')
    serialized = canonical_policy(policy)
    digest = sha256(serialized.encode()).hexdigest()
    row = db.scalar(select(LeavePolicyVersion).where(
        LeavePolicyVersion.policy_sha256 == digest,
        LeavePolicyVersion.restore_epoch.is_(None) if identity['expected_epoch'] is None
        else LeavePolicyVersion.restore_epoch == identity['expected_epoch']))
    if row is not None:
        read_policy_version(db, policy_id=row.id, **identity)
        return row
    row = LeavePolicyVersion(id=secrets.token_hex(32), actor_id=account.id,
        policy_json=serialized, policy_sha256=digest, reason=reason.strip(),
        restore_epoch=identity['expected_epoch'], created_at=datetime.now(UTC))
    db.add(row)
    db.flush()
    db.add(AuditLog(actor=account.username, action='leave_policy_version_saved',
        entity_type='leave_policy_version', entity_id=row.id,
        new_value=serialized, reason=reason.strip()))
    db.flush()
    return row
