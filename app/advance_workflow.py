"""Salary advance workflow with monthly caps and next-payroll tracking."""
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import utcnow, AdvanceInstallment, AdvanceRequest, AuditLog, Employee, PayrollItem, PayrollRun


RESERVING_STATUSES = ("pending", "approved", "paid")


def payroll_month_for_advance(request_date: date) -> date:
    """Return the salary period charged for an advance request.

    The salary period is the request month; payroll is paid on the fifth of the
    following month. This keeps accounting month separate from payout date.
    """
    return date(request_date.year, request_date.month, 1)


def next_payroll_month(request_date: date) -> date:
    if request_date.month == 12:
        return date(request_date.year + 1, 1, 1)
    return date(request_date.year, request_date.month + 1, 1)


def reserved_amount(db: Session, employee_id: int, payroll_month: date, exclude_id: int | None = None) -> Decimal:
    statement = select(func.coalesce(func.sum(AdvanceRequest.amount), 0)).where(
        AdvanceRequest.employee_id == employee_id,
        AdvanceRequest.payroll_month == payroll_month,
        AdvanceRequest.status.in_(RESERVING_STATUSES),
        AdvanceRequest.advance_type == "regular",
    )
    if exclude_id is not None:
        statement = statement.where(AdvanceRequest.id != exclude_id)
    value = db.scalar(statement)
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def advance_cap_basis(db: Session, employee_id: int, payroll_month: date, basic_salary: Decimal) -> Decimal:
    """Base of the regular-advance limit: basic salary plus that payroll month's performance bonus when
    the month's payroll item already exists, otherwise basic salary only (design relayed via Main; this
    docstring is not an approval record). Never invents a bonus: no item means no bonus."""
    bonus = db.scalar(
        select(PayrollItem.performance_bonus)
        .join(PayrollRun, PayrollRun.id == PayrollItem.payroll_run_id)
        .where(PayrollItem.employee_id == employee_id, PayrollRun.period_start == payroll_month)
        .limit(1)
    )
    return Decimal(str(basic_salary)) + Decimal(str(bonus or 0))


def _audit(db: Session, *, actor: str, action: str, request: AdvanceRequest, old: str, new: str, reason: str) -> None:
    db.add(AuditLog(
        actor=actor,
        action=action,
        entity_type="advance_request",
        entity_id=str(request.id),
        old_value=old,
        new_value=new,
        reason=reason,
    ))


def create_advance(
    db: Session,
    *,
    employee_id: int,
    request_date: date,
    amount: Decimal,
    reason: str,
    advance_type: str,
    actor: str,
    submission_key: str | None = None,
) -> AdvanceRequest:
    if request_date.day < 15 or request_date.day > 22:
        raise ValueError("Advance requests are allowed only from day 15 through day 22.")
    today = date.today()
    if request_date > today:
        raise ValueError("An advance request date cannot be in the future.")
    if (request_date.year, request_date.month) != (today.year, today.month):
        raise ValueError("Advance requests must be recorded in the current month.")
    if advance_type not in {"regular", "installment"}:
        raise ValueError("Invalid advance type.")
    if amount <= 0 or not reason.strip():
        raise ValueError("Enter a positive amount and a reason.")

    employee = db.scalar(
        select(Employee).where(Employee.id == employee_id).with_for_update()
    )
    if employee is None or employee.status != "active":
        raise ValueError("The employee is not active.")
    if submission_key is not None:
        submission_key = submission_key.strip()
        if len(submission_key) < 32 or len(submission_key) > 80:
            raise ValueError("Invalid advance submission key.")
        previous_submission = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "advance_submission_accepted",
                AuditLog.entity_type == "advance_submission",
                AuditLog.entity_id == submission_key,
            ).limit(1)
        )
        if previous_submission is not None:
            try:
                previous_request_id = int(previous_submission.new_value or "")
            except ValueError as exc:
                raise ValueError("The previous advance submission record is invalid.") from exc
            previous_request = db.get(AdvanceRequest, previous_request_id)
            if previous_request is None:
                raise ValueError("The previous advance submission no longer exists.")
            if (previous_request.employee_id != employee_id or previous_request.advance_type != advance_type
                    or Decimal(str(previous_request.amount)) != Decimal(str(amount))):
                raise ValueError("This submission key was already used for a different advance request.")
            return previous_request

    salary = Decimal(str(employee.basic_salary))
    if salary <= 0:
        raise ValueError("The employee must have a positive basic salary.")

    payroll_month = payroll_month_for_advance(request_date)
    if advance_type == "regular":
        cap = (advance_cap_basis(db, employee_id, payroll_month, salary) * Decimal("0.25")).quantize(Decimal("0.01"))
        already_reserved = reserved_amount(db, employee_id, payroll_month)
        if already_reserved + amount > cap:
            raise ValueError(
                f"Monthly advance limit is {cap}; already reserved: {already_reserved}."
            )

    request = AdvanceRequest(
        employee_id=employee_id,
        request_date=request_date,
        payroll_month=payroll_month,
        amount=amount,
        salary_basis=salary,
        advance_type=advance_type,
        reason=reason.strip(),
        status="pending",
        created_by=actor,
    )
    db.add(request)
    db.flush()
    _audit(
        db, actor=actor, action="advance_requested", request=request,
        old="", new=f"{amount}|{payroll_month.isoformat()}",
        reason=reason.strip(),
    )
    if submission_key is not None:
        db.add(AuditLog(
            actor=actor,
            action="advance_submission_accepted",
            entity_type="advance_submission",
            entity_id=submission_key,
            old_value=None,
            new_value=str(request.id),
            reason="Idempotency key for the advance request form.",
        ))
    return request


def create_installments(db: Session, request: AdvanceRequest, installment_count: int, actor: str) -> list[AdvanceInstallment]:
    """Persist the approved repayment schedule exactly once."""
    from app.advance_repayment_schedule import build_repayment_schedule

    existing = db.scalars(
        select(AdvanceInstallment)
        .where(AdvanceInstallment.advance_request_id == request.id)
        .order_by(AdvanceInstallment.installment_number)
    ).all()
    if existing:
        if len(existing) != installment_count:
            raise ValueError("Advance installment schedule already exists with a different count.")
        return existing

    schedule = build_repayment_schedule(
        Decimal(request.amount), request.request_date, installment_count
    )
    rows = [
        AdvanceInstallment(
            advance_request_id=request.id,
            installment_number=item["installment_number"],
            amount=item["amount"],
            payroll_month=item["payroll_month"],
            scheduled_payment_date=item["pay_date"],
            status="scheduled",
            created_at=utcnow(),
        )
        for item in schedule
    ]
    db.add_all(rows)
    db.flush()
    return rows


def _lock_request(db: Session, request: AdvanceRequest) -> AdvanceRequest:
    """Lock the advance row and re-read it, so a stale in-session copy never decides the status check."""
    locked = db.scalar(
        select(AdvanceRequest).where(AdvanceRequest.id == request.id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if locked is None:
        raise ValueError("The advance request no longer exists.")
    return locked


def approve_advance(db: Session, request: AdvanceRequest, actor: str, installment_count: int = 1) -> None:
    request = _lock_request(db, request)
    if request.status != "pending":
        raise ValueError("Only a pending advance can be approved.")
    employee = db.scalar(
        select(Employee).where(Employee.id == request.employee_id).with_for_update()
    )
    if employee is None or employee.status != "active":
        raise ValueError("The employee is not active.")
    if request.advance_type == "regular":
        cap = (advance_cap_basis(db, request.employee_id, request.payroll_month, Decimal(request.salary_basis)) * Decimal("0.25")).quantize(Decimal("0.01"))
        already_reserved = reserved_amount(
            db, request.employee_id, request.payroll_month, exclude_id=request.id
        )
        if already_reserved + Decimal(request.amount) > cap:
            raise ValueError("The monthly advance limit would be exceeded.")
    if request.advance_type == "regular":
        if installment_count != 1:
            raise ValueError("Regular advances must be deducted as one monthly advance.")
    elif installment_count < 1 or installment_count > 36:
        raise ValueError("Installment count must be between 1 and 36.")
    create_installments(db, request, installment_count, actor)
    request.status = "approved"
    request.approved_by = actor
    request.approved_at = utcnow()
    _audit(
        db, actor=actor, action="advance_approved", request=request,
        old="pending", new="approved", reason=request.reason,
    )


def reject_advance(db: Session, request: AdvanceRequest, actor: str, reason: str) -> None:
    request = _lock_request(db, request)
    if request.status != "pending":
        raise ValueError("Only a pending advance can be rejected.")
    if not reason.strip():
        raise ValueError("A rejection reason is required.")
    request.status = "rejected"
    request.rejected_by = actor
    request.rejected_at = utcnow()
    request.rejection_reason = reason.strip()
    _audit(
        db, actor=actor, action="advance_rejected", request=request,
        old="pending", new="rejected", reason=reason.strip(),
    )


def mark_advance_paid(db: Session, request: AdvanceRequest, actor: str) -> None:
    request = _lock_request(db, request)
    if request.status != "approved":
        raise ValueError("Only an approved advance can be marked as paid.")
    request.status = "paid"
    request.paid_by = actor
    request.paid_at = utcnow()
    _audit(
        db, actor=actor, action="advance_paid", request=request,
        old="approved", new="paid", reason="Payment confirmed by HR administrator.",
    )
