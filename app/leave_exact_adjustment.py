"""Unwired exact manual-adjustment kernel for the new leave module.

Not an HTTP approval endpoint, accrual, carry, cashout, expiry or close workflow.
Caller must obtain human review before invoking with the reviewed evidence hash.
Own transaction prevents unrelated ORM state from being flushed or committed.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.leave_exact_amount import ledger_amount, exact_text

from app.models import (
    AdminAccount,
    AdminSession,
    AuditLog,
    Employee,
    LeaveBalanceTransaction,
    LeaveType,
)


@dataclass(frozen=True)
class ExactAdjustment:
    employee_id: int
    leave_type_id: int
    days_delta: Decimal
    reason: str
    operation_key: str
    earning_year: int
    released_on: date


def _validate(proposal):
    if not isinstance(proposal, ExactAdjustment):
        raise TypeError('An explicit adjustment proposal is required.')
    for value in (proposal.employee_id, proposal.leave_type_id, proposal.earning_year):
        if type(value) is not int or value < 1:
            raise ValueError('Positive resolved identities and earning year required.')
    if proposal.earning_year > 9999 or type(proposal.released_on) is not date:
        raise ValueError('Explicit earning year and release date required.')
    if (not isinstance(proposal.days_delta, Decimal)
            or not proposal.days_delta.is_finite() or proposal.days_delta == 0):
        raise ValueError('Finite nonzero Decimal days required.')
    for value, limit in ((proposal.reason, 4000), (proposal.operation_key, 160)):
        if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > limit:
            raise ValueError('Explicit bounded reason and operation key required.')


def _hash(value):
    return hashlib.sha256(json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _amount(value):
    # Canonical numeric encoding without Decimal.normalize's context rounding.
    result = format(value, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def _source(proposal):
    return _hash([proposal.employee_id, proposal.leave_type_id, _amount(proposal.days_delta),
                  proposal.reason, proposal.operation_key, proposal.earning_year,
                  proposal.released_on.isoformat()])


def adjustment_evidence_digest(db: Session, proposal: ExactAdjustment) -> str:
    """Review snapshot only, no flush, default identity or inferred zero entity."""
    _validate(proposal)
    with db.no_autoflush:
        employee = db.execute(select(Employee.code, Employee.full_name).where(
            Employee.id == proposal.employee_id)).first()
        leave_type = db.execute(select(LeaveType.code, LeaveType.active, LeaveType.tracks_balance).where(
            LeaveType.id == proposal.leave_type_id)).first()
        if employee is None or leave_type is None or not leave_type.active or not leave_type.tracks_balance:
            raise ValueError('Current employee and active balance-tracking type required.')
        rows = db.execute(select(
            LeaveBalanceTransaction.id, LeaveBalanceTransaction.transaction_type,
            LeaveBalanceTransaction.days_delta, LeaveBalanceTransaction.exact_numerator,
            LeaveBalanceTransaction.exact_denominator, LeaveBalanceTransaction.leave_record_id,
            LeaveBalanceTransaction.source_operation_key, LeaveBalanceTransaction.source_digest,
            LeaveBalanceTransaction.earning_year, LeaveBalanceTransaction.released_on,
            LeaveBalanceTransaction.reason, LeaveBalanceTransaction.created_by).where(
            LeaveBalanceTransaction.employee_id == proposal.employee_id,
            LeaveBalanceTransaction.leave_type_id == proposal.leave_type_id
        ).order_by(LeaveBalanceTransaction.id)).all()
        payload = []
        for row in rows:
            amount = ledger_amount(row)
            encoded = _amount(amount) if isinstance(amount, Decimal) else exact_text(amount)
            payload.append([row.id, row.transaction_type, encoded,
                            row.leave_record_id, row.source_operation_key, row.source_digest,
                            row.earning_year, row.released_on.isoformat() if row.released_on else None,
                            row.reason, row.created_by])
        return _hash([_source(proposal), employee.code, employee.full_name,
                      leave_type.code, leave_type.active, leave_type.tracks_balance, payload])


def apply_reviewed_exact_adjustment(engine, *, proposal: ExactAdjustment,
                                    actor_id: int, session_id: str,
                                    session_version: int, reviewed_digest: str) -> int:
    """Commit manual day adjustment+immutable audit together, or nothing.

    Retry of identical operation returns its existing ID; altered proposals or
    stale balances fail closed. Not a replacement for request/CSRF/human review.
    Current authenticated HR account/session must still exist inside transaction.
    No caller session, role string or supplied model authorization is trusted.
    """
    _validate(proposal)
    if (type(actor_id) is not int or actor_id < 1 or type(session_version) is not int
            or not isinstance(session_id, str) or not session_id
            or not isinstance(reviewed_digest, str) or len(reviewed_digest) != 64
            or any(c not in '0123456789abcdef' for c in reviewed_digest)):
        raise ValueError('Current session identity and reviewed digest required.')
    if engine.dialect.name != 'postgresql':
        raise RuntimeError('This kernel requires migrated PostgreSQL locking semantics.')
    with Session(engine) as db, db.begin():
        db.execute(text("SET LOCAL lock_timeout = '3s'"))
        # Conservative serialization also excludes writes through old leave paths.
        db.execute(text('LOCK TABLE leave_balance_transactions, employees, leave_types '
                        'IN SHARE ROW EXCLUSIVE MODE'))
        actor = db.scalar(select(AdminAccount).where(AdminAccount.id == actor_id).with_for_update())
        login = db.scalar(select(AdminSession).where(AdminSession.id == session_id).with_for_update())
        if (actor is None or login is None or login.admin_account_id != actor_id
                or not actor.active or actor.role != 'system_admin'
                or actor.must_change_password or actor.pending_credential_reset
                or actor.session_version != session_version):
            raise PermissionError('Current HR manager session is required.')
        source_digest = _source(proposal)
        previous = db.scalar(select(LeaveBalanceTransaction).where(
            LeaveBalanceTransaction.source_operation_key == proposal.operation_key))
        if previous is not None:
            if previous.source_digest != source_digest or previous.created_by != actor.username:
                raise ValueError('Operation identity conflicts with recorded source.')
            return previous.id
        if adjustment_evidence_digest(db, proposal) != reviewed_digest:
            raise ValueError('Reviewed leave evidence is stale; review again.')
        entry = LeaveBalanceTransaction(employee_id=proposal.employee_id,
            leave_type_id=proposal.leave_type_id, transaction_type='adjustment',
            days_delta=proposal.days_delta, reason=proposal.reason, created_by=actor.username,
            source_operation_key=proposal.operation_key, source_digest=source_digest,
            earning_year=proposal.earning_year, released_on=proposal.released_on)
        db.add(entry)
        db.flush()
        db.add(AuditLog(actor=actor.username, action='leave_exact_manual_adjustment',
            entity_type='employee_leave_balance',
            entity_id=f'{proposal.employee_id}:{proposal.leave_type_id}',
            old_value=reviewed_digest, new_value=source_digest, reason=proposal.reason))
        db.flush()
        return entry.id
