"""Business rules for administrator-managed leave records and balance history."""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.leave_balance_rules import preview_manual_balance, validate_manual_balance_change
from app.models import (
    AuditLog,
    LeaveBalanceTransaction,
    LeaveRecord,
    LeaveType,
    utcnow,
)


def leave_balance(db: Session, employee_id: int, leave_type_id: int) -> Decimal:
    value = db.scalar(
        select(func.coalesce(func.sum(LeaveBalanceTransaction.days_delta), 0)).where(
            LeaveBalanceTransaction.employee_id == employee_id,
            LeaveBalanceTransaction.leave_type_id == leave_type_id,
        )
    )
    result = Decimal(str(value if value is not None else 0))
    if not result.is_finite():
        raise ValueError("Leave balance evidence must be finite.")
    return result


def add_balance_entry(
    db: Session,
    *,
    employee_id: int,
    leave_type: LeaveType,
    transaction_type: str,
    days_delta: Decimal,
    reason: str,
    actor: str,
    leave_record_id: int | None = None,
) -> LeaveBalanceTransaction:
    if not leave_type.tracks_balance:
        raise ValueError("This leave type does not use a balance.")
    if not reason or not reason.strip():
        raise ValueError("A reason is required for every balance transaction.")
    if not isinstance(days_delta, Decimal) or not days_delta.is_finite():
        raise ValueError("Finite Decimal days are required.")
    if days_delta == 0:
        raise ValueError("The balance change must be greater than zero or less than zero.")
    if not isinstance(transaction_type, str):
        raise ValueError("Explicit transaction type required.")
    if transaction_type in {"opening", "adjustment"}:
        validate_manual_balance_change(transaction_type, days_delta)
    elif transaction_type not in {"leave_use", "leave_reversal"}:
        raise ValueError("Unknown leave balance transaction type.")
    if transaction_type == "opening":
        exists = db.scalar(
            select(LeaveBalanceTransaction.id).where(
                LeaveBalanceTransaction.employee_id == employee_id,
                LeaveBalanceTransaction.leave_type_id == leave_type.id,
                LeaveBalanceTransaction.transaction_type == "opening",
            ).limit(1)
        )
        if exists is not None:
            raise ValueError("An opening balance was already recorded; use an adjustment instead.")
    previous = leave_balance(db, employee_id, leave_type.id)
    if transaction_type in {"opening", "adjustment"}:
        # Same exact-result/resource check as AI preview, before any ledger/audit mutation.
        preview_manual_balance(previous, transaction_type, days_delta)
    entry = LeaveBalanceTransaction(
        employee_id=employee_id,
        leave_type_id=leave_type.id,
        leave_record_id=leave_record_id,
        transaction_type=transaction_type,
        days_delta=days_delta,
        reason=reason.strip(),
        created_by=actor,
    )
    db.add(entry)
    db.flush()
    current = leave_balance(db, employee_id, leave_type.id)
    db.add(AuditLog(
        actor=actor,
        action=f"leave_balance_{transaction_type}",
        entity_type="employee_leave_balance",
        entity_id=f"{employee_id}:{leave_type.id}",
        old_value=str(previous),
        new_value=str(current),
        reason=reason.strip(),
    ))
    return entry


def confirm_leave(db: Session, record: LeaveRecord, actor: str) -> None:
    if record.status != "draft":
        raise ValueError("Only a draft leave record can be confirmed.")
    leave_type = db.get(LeaveType, record.leave_type_id)
    if leave_type is None or not leave_type.active:
        raise ValueError("The selected leave type is not active.")
    if leave_type.tracks_balance:
        add_balance_entry(
            db,
            employee_id=record.employee_id,
            leave_type=leave_type,
            transaction_type="leave_use",
            days_delta=Decimal(record.charged_days).copy_negate(),
            reason=f"Confirmed leave record #{record.id}",
            actor=actor,
            leave_record_id=record.id,
        )
    record.status = "confirmed"
    record.confirmed_by = actor
    record.confirmed_at = utcnow()
    db.add(AuditLog(
        actor=actor,
        action="leave_confirmed",
        entity_type="leave_record",
        entity_id=str(record.id),
        old_value="draft",
        new_value="confirmed",
        reason=record.reason,
    ))


def cancel_leave(db: Session, record: LeaveRecord, actor: str, reason: str) -> None:
    if record.status == "cancelled":
        raise ValueError("This leave record is already cancelled.")
    if record.status == "confirmed":
        leave_type = db.get(LeaveType, record.leave_type_id)
        if leave_type and leave_type.tracks_balance:
            add_balance_entry(
                db,
                employee_id=record.employee_id,
                leave_type=leave_type,
                transaction_type="leave_reversal",
                days_delta=Decimal(record.charged_days),
                reason=f"Cancellation of leave record #{record.id}: {reason.strip()}",
                actor=actor,
                leave_record_id=record.id,
            )
    old_status = record.status
    record.status = "cancelled"
    record.cancelled_by = actor
    record.cancelled_at = utcnow()
    record.cancellation_reason = reason.strip()
    db.add(AuditLog(
        actor=actor,
        action="leave_cancelled",
        entity_type="leave_record",
        entity_id=str(record.id),
        old_value=old_status,
        new_value="cancelled",
        reason=reason.strip(),
    ))
