"""Payroll lifecycle and calculations using the approved HR policy."""
from calendar import monthrange
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import CAIRO_TZ, utcnow
from app.models import (
    AdvanceInstallment,
    AdvanceRequest,
    AttendanceDailyRecord,
    AttendanceMonth,
    AuditLog,
    Department,
    Employee,
    EmployeeChange,
    EmployeeServicePeriod,
    LeaveRecord,
    LeaveType,
    PayrollItem,
    PayrollPayment,
    PayrollRun,
    StaffAction,
)
from app.payroll_math import absence_deduction, lateness_deduction, money, overtime_hourly_rate


MANUAL_ITEM_FIELDS = {
    "attendance_bonus",
    "performance_bonus",
    "occupancy_bonus",
    "overtime_hours",
    "other_bonus",
    "absence_days",
    "lateness_deduction",
    "insurance_share",
    "taxes",
    "other_deductions",
    "note",
}


def payroll_period(year: int, month: int) -> tuple[date, date, date]:
    if month < 1 or month > 12:
        raise ValueError("Invalid payroll month.")
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    if month == 12:
        pay_date = date(year + 1, 1, 5)
    else:
        pay_date = date(year, month + 1, 5)
    return start, end, pay_date


def _audit(db: Session, *, actor: str, action: str, entity_type: str, entity_id: str,
           old_value: str | None, new_value: str | None, reason: str) -> None:
    db.add(AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_value=old_value,
        new_value=new_value,
        reason=reason,
    ))


def _active_days(periods: list[EmployeeServicePeriod], start: date, end: date) -> int:
    days = set()
    for period in periods:
        period_start = max(start, period.start_date)
        period_end = min(end, period.end_date or end)
        if period_end < period_start:
            continue
        for offset in range((period_end - period_start).days + 1):
            days.add(period_start.fromordinal(period_start.toordinal() + offset))
    return len(days)


def _coerce_employee_history_value(field_name: str, value):
    if value is None:
        return None
    if field_name in {"basic_salary", "daily_hours"}:
        return Decimal(str(value))
    if field_name in {"department_id", "branch_id"}:
        return int(value)
    return str(value)


def _employee_snapshot_on_date(
    db: Session,
    employee: Employee,
    effective_date: date,
) -> tuple[dict, list[EmployeeChange]]:
    """Resolve profile values as of a date by reversing later effective changes.

    Employee rows hold the latest entered value. Payroll must not silently use a
    later-effective salary, hours, department, position, or name for an earlier
    month. Inconsistent/out-of-order audit history fails closed.
    """
    changes = db.scalars(
        select(EmployeeChange)
        .where(EmployeeChange.employee_id == employee.id)
        .order_by(EmployeeChange.effective_date.desc(), EmployeeChange.id.desc())
    ).all()
    by_field: dict[str, list[EmployeeChange]] = {}
    for change in changes:
        by_field.setdefault(change.field_name, []).append(change)

    snapshot = {}
    for field_name in ("full_name", "department_id", "position", "basic_salary", "daily_hours"):
        value = _coerce_employee_history_value(field_name, getattr(employee, field_name))
        for change in by_field.get(field_name, []):
            expected = _coerce_employee_history_value(field_name, change.new_value)
            if value != expected:
                raise ValueError(
                    f"Employee #{employee.id} has inconsistent effective-date history for "
                    f"{field_name}; reconcile the employee change history before payroll."
                )
            if change.effective_date > effective_date:
                value = _coerce_employee_history_value(field_name, change.old_value)
            else:
                break
        snapshot[field_name] = value
    return snapshot, changes


def _attendance_lateness(
    records: list[AttendanceDailyRecord],
    salary_basis: Decimal,
    daily_hours: Decimal,
) -> tuple[int, int, Decimal]:
    late_total = 0
    early_total = 0
    deduction = Decimal("0.00")
    for record in records:
        if record.status != "approved":
            continue
        scheduled_start = record.scheduled_start_at
        scheduled_end = record.scheduled_end_at
        actual_start = record.approved_clock_in or record.first_clock_in or record.program_clock_in
        actual_end = record.approved_clock_out or record.last_clock_out or record.program_clock_out
        late = 0
        early = 0
        if scheduled_start is not None and actual_start is not None:
            late = max(0, int((actual_start - scheduled_start).total_seconds() // 60))
        if scheduled_end is not None and actual_end is not None:
            early = max(0, int((scheduled_end - actual_end).total_seconds() // 60))
        late_total += late
        early_total += early
        # Grace is applied separately for every approved workday.
        deduction += lateness_deduction(
            salary_basis,
            daily_hours,
            late_minutes=late,
            early_minutes=early,
            late_grace_minutes=10,
            early_grace_minutes=5,
        )
    return late_total, early_total, money(deduction)


class AbsenceLeaveConflictError(ValueError):
    """Approval blocked: unreviewed absence/paid-leave conflicts."""

    def __init__(self, employee_codes: list[str]):
        super().__init__("Unreviewed absence/paid-leave conflicts: " + ", ".join(employee_codes))
        self.employee_codes = employee_codes


def absence_leave_conflicts(db: Session, item: PayrollItem, run: PayrollRun) -> list[LeaveRecord]:
    """Confirmed paid leave overlapping the run period while absence days are entered.

    Absence days are a manual month-level entry; a confirmed paid leave record in
    the same period means the deduction may double-charge the employee. This is a
    review trigger, never an automatic verdict: HR reviews and either corrects the
    absence days or explicitly confirms the deduction stands.
    """
    if Decimal(item.absence_days) <= 0:
        return []
    return db.scalars(
        select(LeaveRecord)
        .join(LeaveType, LeaveType.id == LeaveRecord.leave_type_id)
        .where(
            LeaveRecord.employee_id == item.employee_id,
            LeaveRecord.status == "confirmed",
            LeaveType.is_paid.is_(True),
            LeaveRecord.start_date <= run.period_end,
            LeaveRecord.end_date >= run.period_start,
        )
        .order_by(LeaveRecord.start_date)
    ).all()


def _absence_leave_fingerprint(
    item: PayrollItem, run: PayrollRun, conflicts: list[LeaveRecord]
) -> str:
    """Bind a review confirmation to the exact inputs it covered.

    Any later change to the conflicting leave set (added, cancelled, edited) or
    to the absence days invalidates the fingerprint, so approval requires a
    fresh human review instead of silently reusing a stale confirmation.
    """
    import hashlib
    leave_parts = ",".join(
        f"{leave.id}:{leave.start_date}:{leave.end_date}" for leave in conflicts
    )
    raw = f"{item.employee_id}|{run.period_start}|{Decimal(item.absence_days)}|{leave_parts}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def review_absence_leave_conflict(db: Session, item: PayrollItem, actor: str) -> None:
    run = db.get(PayrollRun, item.payroll_run_id)
    if run is None or run.status not in {"draft", "reopened"}:
        raise ValueError("Conflict review is possible only while payroll is draft or reopened.")
    conflicts = absence_leave_conflicts(db, item, run)
    if not conflicts:
        raise ValueError("لا يوجد تعارض غياب مع إجازة مدفوعة قائم لهذا البند.")
    item.absence_leave_reviewed = True
    item.absence_leave_review_fingerprint = _absence_leave_fingerprint(item, run, conflicts)
    _audit(
        db, actor=actor, action="absence_leave_conflict_reviewed",
        entity_type="payroll_item", entity_id=str(item.id),
        old_value="false", new_value="true",
        reason="HR reviewed absence deduction against confirmed paid leave.",
    )


def recompute_payroll_item(item: PayrollItem, run: PayrollRun) -> None:
    overtime_rate = overtime_hourly_rate(
        Decimal(item.salary_basis), Decimal(item.performance_bonus), Decimal(item.daily_hours)
    )
    if item.overtime_daily_digest is None:
        item.overtime_amount = (
            money(overtime_rate * Decimal(item.overtime_hours))
            if item.overtime_approved else Decimal('0.00')
        )
    # Daily-ledger amount already carries its explicit monthly rounding contract.
    # This pure calculator must not overwrite it using a single monthly divisor.
    item.absence_deduction = absence_deduction(
        Decimal(item.salary_basis),
        run.period_start.year,
        run.period_start.month,
        Decimal(item.absence_days),
    )
    gross = (
        Decimal(item.base_salary)
        + Decimal(item.attendance_bonus)
        + Decimal(item.performance_bonus)
        + Decimal(item.occupancy_bonus)
        + Decimal(item.overtime_amount)
        + Decimal(item.other_bonus)
        + Decimal(item.staff_reward_amount)
    )
    deductions = (
        Decimal(item.absence_deduction)
        + Decimal(item.lateness_deduction)
        + Decimal(item.insurance_share)
        + Decimal(item.taxes)
        + Decimal(item.other_deductions)
        + Decimal(item.advance_deduction)
        + Decimal(item.staff_sanction_amount)
    )
    net = money(gross - deductions)
    if net < 0:
        raise ValueError(
            f"Payroll deductions exceed earnings for employee #{item.employee_id}."
        )
    item.net_pay = net


def create_payroll_run(db: Session, *, year: int, month: int, actor: str) -> PayrollRun:
    start, end, pay_date = payroll_period(year, month)
    attendance_month = db.scalar(
        select(AttendanceMonth).where(
            AttendanceMonth.period_start == start,
            AttendanceMonth.period_end == end,
        )
    )
    if (
        attendance_month is None
        or attendance_month.status != "approved"
        or attendance_month.program_import_batch_id is None
        or attendance_month.fingerprint_import_batch_id is None
    ):
        raise ValueError("Attendance for this exact month must be fully approved before payroll.")
    existing = db.scalar(
        select(PayrollRun.id).where(PayrollRun.period_start == start)
    )
    if existing is not None:
        raise ValueError("A payroll run already exists for this month.")

    service_periods = db.scalars(
        select(EmployeeServicePeriod).where(
            EmployeeServicePeriod.start_date <= end,
            or_(EmployeeServicePeriod.end_date.is_(None), EmployeeServicePeriod.end_date >= start),
        ).order_by(EmployeeServicePeriod.employee_id, EmployeeServicePeriod.start_date)
    ).all()
    periods_by_employee: dict[int, list[EmployeeServicePeriod]] = {}
    for period in service_periods:
        periods_by_employee.setdefault(period.employee_id, []).append(period)

    run = PayrollRun(
        overtime_daily_required=True,
        period_start=start,
        period_end=end,
        pay_date=pay_date,
        status="draft",
        created_by=actor,
    )
    db.add(run)
    db.flush()

    installments = db.execute(
        select(AdvanceInstallment, AdvanceRequest)
        .join(AdvanceRequest, AdvanceRequest.id == AdvanceInstallment.advance_request_id)
        .where(
            AdvanceInstallment.payroll_month == start,
            AdvanceInstallment.status.in_(("scheduled", "reversed")),
            AdvanceInstallment.applied_payroll_run_id.is_(None),
            AdvanceRequest.status == "paid",
        )
        .with_for_update()
    ).all()
    advances_by_employee: dict[int, Decimal] = {}
    for installment, advance in installments:
        advances_by_employee[advance.employee_id] = (
            advances_by_employee.get(advance.employee_id, Decimal("0.00")) + Decimal(installment.amount)
        )

    staff_actions = db.scalars(
        select(StaffAction).where(
            StaffAction.payroll_month == start,
            StaffAction.status == "approved",
            StaffAction.is_financial.is_(True),
            StaffAction.payroll_run_id.is_(None),
            StaffAction.payroll_applied_at.is_(None),
        )
    ).all()
    rewards_by_employee: dict[int, Decimal] = {}
    sanctions_by_employee: dict[int, Decimal] = {}
    for action in staff_actions:
        target = rewards_by_employee if action.kind == "reward" else sanctions_by_employee
        target[action.employee_id] = target.get(action.employee_id, Decimal("0.00")) + Decimal(action.amount)

    days_in_month = monthrange(year, month)[1]
    included_employee_ids = set()
    for employee_id, periods in periods_by_employee.items():
        employee = db.get(Employee, employee_id)
        if employee is None:
            continue
        employee_snapshot, employee_changes = _employee_snapshot_on_date(db, employee, start)
        mid_period_compensation_change = next(
            (
                change for change in employee_changes
                if change.field_name in {"basic_salary", "daily_hours"}
                and start < change.effective_date <= end
            ),
            None,
        )
        if mid_period_compensation_change is not None:
            raise ValueError(
                f"Employee #{employee.id} has a {mid_period_compensation_change.field_name} "
                f"change effective on {mid_period_compensation_change.effective_date}. "
                "Payroll is blocked because mid-month compensation proration requires an "
                "explicit HR policy; no salary rate was guessed."
            )
        salary_basis = Decimal(employee_snapshot["basic_salary"])
        daily_hours = Decimal(employee_snapshot["daily_hours"])
        payable_days = _active_days(periods, start, end)
        base_salary = money(salary_basis * Decimal(payable_days) / Decimal(days_in_month))
        attendance_records = db.scalars(
            select(AttendanceDailyRecord).where(
                AttendanceDailyRecord.employee_id == employee_id,
                AttendanceDailyRecord.work_date >= start,
                AttendanceDailyRecord.work_date <= end,
                AttendanceDailyRecord.status == "approved",
            )
        ).all()
        late_minutes, early_minutes, late_deduction = _attendance_lateness(
            attendance_records, salary_basis, daily_hours
        )
        payroll_item = PayrollItem(
            payroll_run_id=run.id,
            employee_id=employee_id,
            employee_code_snapshot=employee.code,
            employee_name_snapshot=employee_snapshot["full_name"],
            department_name_snapshot=(
                db.get(Department, employee_snapshot["department_id"]).name
                if employee_snapshot["department_id"] is not None
                and db.get(Department, employee_snapshot["department_id"]) is not None
                else ""
            ),
            position_snapshot=employee_snapshot["position"],
            salary_basis=salary_basis,
            base_salary=base_salary,
            daily_hours=daily_hours,
            late_minutes=late_minutes,
            early_minutes=early_minutes,
            lateness_deduction=late_deduction,
            advance_deduction=money(advances_by_employee.get(employee_id, Decimal("0.00"))),
            staff_reward_amount=money(rewards_by_employee.get(employee_id, Decimal("0.00"))),
            staff_sanction_amount=money(sanctions_by_employee.get(employee_id, Decimal("0.00"))),
            net_pay=Decimal("0.00"),
        )
        db.add(payroll_item)
        db.flush()
        included_employee_ids.add(employee_id)
        recompute_payroll_item(payroll_item, run)

    # Reserve only the source records actually included in this run snapshot.
    # A later-paid advance or later-approved staff action must not be consumed
    # when this payroll is closed if it was absent from the payroll item.
    # Installments are not marked applied while a payroll run is still draft.
    # Finalization owns the idempotent application boundary.
    for action in staff_actions:
        if action.employee_id in included_employee_ids:
            action.payroll_run_id = run.id

    _audit(
        db, actor=actor, action="payroll_run_created", entity_type="payroll_run",
        entity_id=str(run.id), old_value=None, new_value=f"{start}|{end}|draft",
        reason="Created after approved attendance for the exact month.",
    )
    return run


def approve_overtime(db: Session, item: PayrollItem, actor: str) -> None:
    run = db.get(PayrollRun, item.payroll_run_id)
    if run is None or run.status not in {"draft", "reopened"}:
        raise ValueError("Overtime can be approved only while payroll is draft or reopened.")
    if run.overtime_daily_required or item.overtime_daily_digest is not None:raise ValueError('Daily overtime requires daily source review, not monthly approval.')
    hours = Decimal(item.overtime_hours or 0)
    if hours <= 0:
        raise ValueError("Positive overtime hours are required before approval.")
    item.overtime_approved = True
    item.overtime_approved_by = actor
    item.overtime_approved_at = utcnow()
    recompute_payroll_item(item, run)
    _audit(
        db, actor=actor, action="overtime_approved", entity_type="payroll_item",
        entity_id=str(item.id), old_value="false", new_value="true",
        reason="HR administrator approved overtime.",
    )


def update_payroll_item(
    db: Session, item: PayrollItem, values: dict, actor: str
) -> None:
    run = db.get(PayrollRun, item.payroll_run_id)
    if run is None or run.status not in {"draft", "reopened"}:
        raise ValueError("Payroll items can be edited only while the run is draft or reopened.")
    paid_total = db.scalar(
        select(func.coalesce(func.sum(PayrollPayment.amount), 0)).where(
            PayrollPayment.payroll_item_id == item.id
        )
    )
    if Decimal(str(paid_total or 0)) > 0:
        raise ValueError("A payroll item with recorded payments cannot be edited; record an adjustment separately.")
    if (run.overtime_daily_required or item.overtime_daily_digest is not None) and 'overtime_hours' in values and Decimal(str(values['overtime_hours'] or 0))!=item.overtime_hours:raise ValueError('Daily overtime hours require an explicit day replacement.')
    old = {}
    new = {}
    for key, value in values.items():
        if key not in MANUAL_ITEM_FIELDS:
            raise ValueError(f"Field {key} cannot be edited.")
        if key == "note":
            normalized = (value or "").strip() or None
        else:
            normalized = Decimal(str(value or 0))
            if normalized < 0:
                raise ValueError("Payroll values cannot be negative.")
        old[key] = str(getattr(item, key))
        new[key] = str(normalized)
        setattr(item, key, normalized)
    if "absence_days" in values and Decimal(old["absence_days"]) != Decimal(new["absence_days"]):
        item.absence_leave_reviewed = False
        item.absence_leave_review_fingerprint = None
    # A reviewed award cannot survive a changed financial basis. Unrelated
    # bonuses are deliberately excluded from that basis and keep approval.
    if any(key in values and Decimal(old[key]) != Decimal(new[key])
           for key in ("overtime_hours", "performance_bonus")):
        item.overtime_approved = False
        item.overtime_approved_by = None
        item.overtime_approved_at = None
        if item.overtime_daily_digest is not None:item.overtime_amount=Decimal('0.00')
    if item.overtime_hours <= 0:
        item.overtime_approved = False
        item.overtime_approved_by = None
        item.overtime_approved_at = None
    recompute_payroll_item(item, run)
    _audit(
        db, actor=actor, action="payroll_item_updated", entity_type="payroll_item",
        entity_id=str(item.id), old_value=str(old), new_value=str(new),
        reason="HR administrator edited payroll values.",
    )


def approve_payroll_run(db: Session, run: PayrollRun, actor: str) -> None:
    if run.status not in {"draft", "reopened"}:
        raise ValueError("Only a draft or reopened payroll run can be approved.")
    attendance_month = db.scalar(
        select(AttendanceMonth).where(
            AttendanceMonth.period_start == run.period_start,
            AttendanceMonth.period_end == run.period_end,
        )
    )
    if attendance_month is None or attendance_month.status != "approved":
        raise ValueError("Attendance approval is required before payroll approval.")
    items = db.scalars(
        select(PayrollItem).where(PayrollItem.payroll_run_id == run.id)
    ).all()
    if not items:
        raise ValueError("A payroll run without employee items cannot be approved.")
    conflicted = []
    for item in items:
        conflicts = absence_leave_conflicts(db, item, run)
        if not conflicts:
            continue
        if not item.absence_leave_reviewed:
            conflicted.append(item.employee_code_snapshot)
            continue
        if item.absence_leave_review_fingerprint != _absence_leave_fingerprint(item, run, conflicts):
            conflicted.append(item.employee_code_snapshot)
    if conflicted:
        raise AbsenceLeaveConflictError(sorted(conflicted))
    for item in items:
        from app.overtime_day_award import item_has_payment
        if item_has_payment(db,item.id):continue # Existing paid snapshot remains frozen.
        if run.overtime_daily_required or item.overtime_daily_digest is not None:
            from app.overtime_day_award import verify_monthly_snapshot
            verify_monthly_snapshot(db,item)
        recompute_payroll_item(item, run)
    old = run.status
    run.status = "approved"
    run.approved_by = actor
    run.approved_at = utcnow()
    _audit(
        db, actor=actor, action="payroll_run_approved", entity_type="payroll_run",
        entity_id=str(run.id), old_value=old, new_value="approved",
        reason="HR administrator approved payroll after review.",
    )


def close_payroll_run(db: Session, run: PayrollRun, actor: str) -> None:
    if run.status != "approved":
        raise ValueError("Only approved payroll can be closed.")
    items = db.scalars(
        select(PayrollItem).where(PayrollItem.payroll_run_id == run.id)
    ).all()
    for item in items:
        from app.overtime_day_award import item_has_payment
        if item_has_payment(db,item.id):continue # Existing paid snapshot remains frozen.
        if run.overtime_daily_required or item.overtime_daily_digest is not None:
            from app.overtime_day_award import verify_monthly_snapshot
            verify_monthly_snapshot(db,item)
    employee_ids = {item.employee_id for item in items}
    now = utcnow()
    run_installments = db.scalars(
        select(AdvanceInstallment)
        .join(AdvanceRequest, AdvanceRequest.id == AdvanceInstallment.advance_request_id)
        .where(
            AdvanceInstallment.payroll_month == run.period_start,
            AdvanceInstallment.status.in_(("scheduled", "reversed")),
            AdvanceInstallment.applied_payroll_run_id.is_(None),
            AdvanceRequest.status == "paid",
            AdvanceRequest.employee_id.in_(employee_ids or {-1}),
        )
        .with_for_update()
    ).all()

    # Payroll items are the approved snapshot. A late payment/status change
    # after draft creation must never silently alter the amount consumed at close.
    scheduled_by_employee: dict[int, Decimal] = {}
    for installment in run_installments:
        advance = db.get(AdvanceRequest, installment.advance_request_id)
        if advance is not None:
            scheduled_by_employee[advance.employee_id] = (
                scheduled_by_employee.get(advance.employee_id, Decimal("0.00"))
                + Decimal(installment.amount)
            )
    item_by_employee = {item.employee_id: item for item in items}
    for employee_id, item in item_by_employee.items():
        expected = money(Decimal(item.advance_deduction))
        actual = money(scheduled_by_employee.get(employee_id, Decimal("0.00")))
        if expected != actual:
            raise ValueError(
                f"Advance deductions changed after payroll draft for employee #{employee_id}; reopen/recreate payroll before closing."
            )

    for installment in run_installments:
        installment.status = "applied"
        installment.applied_payroll_run_id = run.id
        installment.applied_at = now
        installment.reversal_reference = None

    # Some test/runtime sessions intentionally disable autoflush. Flush the
    # installment status changes before counting remaining scheduled rows so
    # a single-installment advance can transition from paid to deducted.
    db.flush()

    advance_ids = {item.advance_request_id for item in run_installments}
    for advance in db.scalars(
        select(AdvanceRequest).where(AdvanceRequest.id.in_(advance_ids or {-1}))
    ).all():
        remaining = db.scalar(
            select(func.count(AdvanceInstallment.id)).where(
                AdvanceInstallment.advance_request_id == advance.id,
                AdvanceInstallment.status == "scheduled",
            )
        ) or 0
        if remaining == 0:
            advance.status = "deducted"
            advance.deducted_at = now
            advance.payroll_run_id = run.id
    for action in db.scalars(
        select(StaffAction).where(
            StaffAction.employee_id.in_(employee_ids or {-1}),
            StaffAction.payroll_month == run.period_start,
            StaffAction.status == "approved",
            StaffAction.is_financial.is_(True),
            StaffAction.payroll_run_id == run.id,
            StaffAction.payroll_applied_at.is_(None),
        )
    ).all():
        action.payroll_applied_at = now
    run.status = "closed"
    run.closed_by = actor
    run.closed_at = now
    _audit(
        db, actor=actor, action="payroll_run_closed", entity_type="payroll_run",
        entity_id=str(run.id), old_value="approved", new_value="closed",
        reason="Payroll period closed after approval.",
    )


def _reverse_advance_installments_for_run(db: Session, run: PayrollRun, actor: str) -> None:
    now = utcnow()
    applied = db.scalars(
        select(AdvanceInstallment)
        .where(AdvanceInstallment.applied_payroll_run_id == run.id, AdvanceInstallment.status == "applied")
        .with_for_update()
    ).all()
    for installment in applied:
        installment.status = "reversed"
        installment.reversal_reference = f"payroll-run:{run.id}:reopened-by:{actor}"
        installment.reversed_at = now
        installment.applied_payroll_run_id = None
        installment.applied_at = None
    for advance_id in {item.advance_request_id for item in applied}:
        advance = db.get(AdvanceRequest, advance_id)
        if advance is not None and advance.status == "deducted":
            advance.status = "paid"
            advance.deducted_at = None
            advance.payroll_run_id = None


def reopen_payroll_run(db: Session, run: PayrollRun, actor: str, reason: str) -> None:
    if run.status != "closed":
        raise ValueError("Only a closed payroll run can be reopened.")
    if not reason.strip():
        raise ValueError("A reason is required to reopen payroll.")
    _reverse_advance_installments_for_run(db, run, actor)
    run.status = "reopened"
    run.overtime_daily_required=True
    run.reopened_by = actor
    run.reopened_at = utcnow()
    run.reopen_reason = reason.strip()
    _audit(
        db, actor=actor, action="payroll_run_reopened", entity_type="payroll_run",
        entity_id=str(run.id), old_value="closed", new_value="reopened",
        reason=reason.strip(),
    )


def payment_totals(db: Session, item: PayrollItem) -> tuple[Decimal, Decimal, str]:
    paid = db.scalar(
        select(func.coalesce(func.sum(PayrollPayment.amount), 0)).where(
            PayrollPayment.payroll_item_id == item.id,
            PayrollPayment.superseded_by_payment_id.is_(None),
        )
    )
    paid_total = money(Decimal(str(paid or 0)))
    due = money(max(Decimal("0.00"), Decimal(item.net_pay) - paid_total))
    if paid_total <= 0:
        status = "unpaid"
    elif paid_total < Decimal(item.net_pay):
        status = "partially_paid"
    else:
        status = "fully_paid"
    return paid_total, due, status


def add_payroll_payment(
    db: Session,
    item: PayrollItem,
    *,
    amount: Decimal,
    paid_date: date,
    method: str,
    reference: str | None,
    actor: str,
) -> PayrollPayment:
    run = db.get(PayrollRun, item.payroll_run_id)
    if run is None or run.status != "closed":
        raise ValueError("Payments can be recorded only after payroll is closed.")
    if run.overtime_daily_required or item.overtime_daily_digest is not None:
        from app.overtime_day_award import verify_frozen_monthly_snapshot
        verify_frozen_monthly_snapshot(db,item)
    if method not in {"cash", "bank_transfer"}:
        raise ValueError("Invalid payment method.")
    if amount <= 0:
        raise ValueError("Payment amount must be positive.")
    paid_total, due, _ = payment_totals(db, item)
    if amount > due:
        raise ValueError(f"Payment exceeds the remaining amount of {due}.")
    payment = PayrollPayment(
        payroll_item_id=item.id,
        amount=money(amount),
        paid_at=datetime.combine(paid_date, time(12, 0), tzinfo=CAIRO_TZ),
        method=method,
        reference=(reference or "").strip() or None,
        created_by=actor,
    )
    db.add(payment)
    db.flush()
    _audit(
        db, actor=actor, action="payroll_payment_recorded", entity_type="payroll_payment",
        entity_id=str(payment.id), old_value=str(paid_total),
        new_value=str(paid_total + amount), reason=reference or method,
    )
    return payment


def correct_payroll_payment(
    db: Session,
    payment: PayrollPayment,
    *,
    corrected_amount: Decimal,
    reason: str,
    actor: str,
) -> PayrollPayment:
    """Replace a mistaken payment record with a corrected one, atomically.

    Bookkeeping only: no money moves. The original entry is kept and marked
    superseded by the new correcting entry, which links back to it and carries
    the recorded reason. Never edits or deletes the original record.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to correct a payment record.")
    if payment.superseded_by_payment_id is not None:
        raise ValueError("This payment record was already corrected; correct the latest active record instead.")
    corrected_amount = Decimal(str(corrected_amount))
    # Guard before any arithmetic: NaN/Infinity and values beyond the
    # Numeric(12, 2) column capacity raise InvalidOperation instead of a
    # clean validation error.
    if not corrected_amount.is_finite() or corrected_amount > Decimal("9999999999.99"):
        raise ValueError("Corrected amount is outside the allowed range.")
    if corrected_amount <= 0:
        raise ValueError("Corrected amount must be positive.")
    if money(corrected_amount) == money(Decimal(payment.amount)):
        raise ValueError("Corrected amount equals the recorded amount; nothing to correct.")
    item = db.get(PayrollItem, payment.payroll_item_id)
    if item is None:
        raise ValueError("Payroll item not found for this payment.")
    run = db.get(PayrollRun, item.payroll_run_id)
    if run is None or run.status != "closed":
        raise ValueError("Payments can be corrected only after payroll is closed.")
    paid_total, _, _ = payment_totals(db, item)
    new_total = money(paid_total - Decimal(payment.amount) + corrected_amount)
    if new_total > Decimal(item.net_pay):
        raise ValueError(
            f"Corrected amount exceeds the remaining amount of {money(Decimal(item.net_pay) - (paid_total - Decimal(payment.amount)))}."
        )
    correction = PayrollPayment(
        payroll_item_id=payment.payroll_item_id,
        amount=money(corrected_amount),
        paid_at=payment.paid_at,
        method=payment.method,
        reference=payment.reference,
        created_by=actor,
        correction_of_id=payment.id,
        correction_reason=reason,
    )
    db.add(correction)
    db.flush()
    payment.superseded_by_payment_id = correction.id
    db.flush()
    _audit(
        db, actor=actor, action="payroll_payment_corrected", entity_type="payroll_payment",
        entity_id=str(payment.id), old_value=str(payment.amount),
        new_value=f"{correction.id}:{correction.amount}",
        reason=reason,
    )
    return correction
