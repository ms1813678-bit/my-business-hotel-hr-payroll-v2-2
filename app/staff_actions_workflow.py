"""HR-administered employee sanctions and rewards with acknowledgement history."""
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import utcnow, AuditLog, Employee, StaffAction
from app.open_month import place_month


SANCTION_CATEGORIES = {
    "verbal_notice", "first_warning", "final_warning",
    "salary_deduction", "suspension", "custom",
}
# Reward types from the 19-item list relayed via Main (comment is not an approval record); 13 leave-day
# reward and 18 real promotion/raise are deliberately NOT reward categories. Types 1-11 always carry money into payroll.
FINANCIAL_REWARD_CATEGORIES = {
    "financial_reward",
    "reward_outstanding_performance",
    "reward_attendance_commitment",
    "reward_occupancy_sales",
    "reward_employee_of_month",
    "reward_guest_service",
    "reward_exceptional_overtime",
    "reward_season_holiday",
    "reward_service_years",
    "reward_suggestion_savings",
    "reward_training_allowance"
}
NONFINANCIAL_REWARD_CATEGORIES = {
    "gift_voucher", "certificate", "commendation", "thank_letter",
    "promotion_recommendation", "custom",
}
REWARD_CATEGORIES = FINANCIAL_REWARD_CATEGORIES | NONFINANCIAL_REWARD_CATEGORIES

CATEGORY_LABELS_AR = {
    "verbal_notice": "تنبيه شفهي",
    "first_warning": "إنذار أول",
    "final_warning": "إنذار نهائي",
    "salary_deduction": "خصم من الراتب",
    "suspension": "إيقاف عن العمل",
    "financial_reward": "مكافأة مالية",
    "reward_outstanding_performance": "مكافأة أداء متميز",
    "reward_attendance_commitment": "مكافأة حضور والتزام",
    "reward_occupancy_sales": "مكافأة إشغال/مبيعات",
    "reward_employee_of_month": "موظف الشهر",
    "reward_guest_service": "خدمة ضيف متميزة",
    "reward_exceptional_overtime": "عمل إضافي استثنائي أو طوارئ",
    "reward_season_holiday": "موسم أو عيد",
    "reward_service_years": "سنوات الخدمة",
    "reward_suggestion_savings": "اقتراح تحسين أو توفير تكلفة",
    "reward_training_allowance": "بدل أو مكافأة تدريب وتطوير",
    "gift_voucher": "هدية عينية أو قسيمة شراء",
    "thank_letter": "خطاب شكر في الملف",
    "certificate": "شهادة تقدير",
    "commendation": "إشادة",
    "promotion_recommendation": "توصية بالترقية",
    "custom": "نوع مخصص",
}


def create_staff_action(
    db: Session,
    *,
    employee_id: int,
    kind: str,
    category: str,
    title: str,
    is_financial: bool,
    amount: Decimal | None,
    payroll_month,
    reason: str,
    actor: str,
    today: date | None = None,
) -> StaffAction:
    if kind not in {"sanction", "reward"}:
        raise ValueError("Invalid staff action type.")
    allowed = SANCTION_CATEGORIES if kind == "sanction" else REWARD_CATEGORIES
    if category not in allowed:
        raise ValueError("Invalid category for this action.")
    if not title.strip() or not reason.strip():
        raise ValueError("A title and reason are required.")
    employee = db.scalar(
        select(Employee).where(Employee.id == employee_id).with_for_update()
    )
    if employee is None or employee.status != "active":
        raise ValueError("The employee is not active.")
    if category == "salary_deduction" and not is_financial:
        raise ValueError("A salary deduction must have a financial amount.")
    if category in FINANCIAL_REWARD_CATEGORIES and not is_financial:
        raise ValueError("A financial reward must have a financial amount.")
    if is_financial:
        if amount is None or payroll_month is None:
            raise ValueError("Financial actions need a positive amount and payroll month.")
        amount = Decimal(str(amount)).quantize(Decimal("0.01"))
        if amount <= 0:
            raise ValueError("Financial action amount must be positive.")
        if payroll_month.day != 1:
            raise ValueError("Payroll month must be entered as the first day of the month.")
        payroll_month, retroactive_for = place_month(db, payroll_month, today)
        duplicate = db.scalar(
            select(StaffAction.id).where(
                StaffAction.employee_id == employee_id,
                StaffAction.kind == kind,
                StaffAction.category == category,
                StaffAction.title == title.strip(),
                StaffAction.is_financial.is_(True),
                StaffAction.amount == amount,
                StaffAction.payroll_month == payroll_month,
                StaffAction.reason == reason.strip(),
                StaffAction.status.in_(("draft", "approved")),
            ).limit(1)
        )
        if duplicate is not None:
            raise ValueError(
                "An identical active financial action already exists for this employee and payroll month."
            )
    elif amount is not None or payroll_month is not None:
        raise ValueError("Non-financial actions must not include an amount or payroll month.")
    else:
        retroactive_for = None

    item = StaffAction(
        employee_id=employee_id,
        kind=kind,
        category=category,
        title=title.strip(),
        is_financial=is_financial,
        amount=amount,
        payroll_month=payroll_month,
        retroactive_for_month=retroactive_for,
        reason=reason.strip(),
        status="draft",
        created_by=actor,
        acknowledgement_status="pending",
    )
    db.add(item)
    db.flush()
    db.add(AuditLog(
        actor=actor,
        action=f"{kind}_created",
        entity_type="staff_action",
        entity_id=str(item.id),
        new_value=f"{category}|{amount or ''}|{payroll_month or ''}"
                  + (f"|retroactive for {retroactive_for}" if retroactive_for else ""),
        reason=reason.strip(),
    ))
    return item


def approve_staff_action(db: Session, item: StaffAction, actor: str, today: date | None = None) -> None:
    if item.status != "draft":
        raise ValueError("Only draft actions can be approved.")
    employee = db.scalar(
        select(Employee).where(Employee.id == item.employee_id).with_for_update()
    )
    if employee is None or employee.status != "active":
        raise ValueError("The employee is not active.")
    if item.is_financial and item.payroll_run_id is None:
        # The month may have closed between drafting and approval: place it again, never lose it.
        placed, retroactive_for = place_month(db, item.payroll_month, today)
        if placed != item.payroll_month:
            db.add(AuditLog(
                actor=actor, action=f"{item.kind}_month_moved", entity_type="staff_action",
                entity_id=str(item.id), old_value=str(item.payroll_month), new_value=str(placed),
                reason=item.reason,
            ))
            item.retroactive_for_month = item.retroactive_for_month or retroactive_for
            item.payroll_month = placed
    if item.is_financial:
        duplicate = db.scalar(
            select(StaffAction.id).where(
                StaffAction.id != item.id,
                StaffAction.employee_id == item.employee_id,
                StaffAction.kind == item.kind,
                StaffAction.category == item.category,
                StaffAction.title == item.title,
                StaffAction.is_financial.is_(True),
                StaffAction.amount == item.amount,
                StaffAction.payroll_month == item.payroll_month,
                StaffAction.reason == item.reason,
                StaffAction.status.in_(("draft", "approved")),
            ).limit(1)
        )
        if duplicate is not None:
            raise ValueError("An identical active financial action already exists.")
    item.status = "approved"
    item.approved_by = actor
    item.approved_at = utcnow()
    db.add(AuditLog(
        actor=actor,
        action=f"{item.kind}_approved",
        entity_type="staff_action",
        entity_id=str(item.id),
        old_value="draft",
        new_value="approved",
        reason=item.reason,
    ))
    if item.is_financial and item.payroll_run_id is None:
        from app.payroll_workflow import refresh_draft_for_month
        refresh_draft_for_month(db, item.payroll_month, actor)  # a draft of that month takes it in


def cancel_staff_action(db: Session, item: StaffAction, actor: str, reason: str) -> None:
    if item.status == "cancelled":
        raise ValueError("This action is already cancelled.")
    if item.payroll_run_id is not None or item.payroll_applied_at is not None:
        raise ValueError("An action reserved by or applied to payroll cannot be cancelled here.")
    if not reason.strip():
        raise ValueError("A cancellation reason is required.")
    old_status = item.status
    item.status = "cancelled"
    item.cancelled_by = actor
    item.cancelled_at = utcnow()
    item.cancellation_reason = reason.strip()
    db.add(AuditLog(
        actor=actor,
        action=f"{item.kind}_cancelled",
        entity_type="staff_action",
        entity_id=str(item.id),
        old_value=old_status,
        new_value="cancelled",
        reason=reason.strip(),
    ))


def record_employee_acknowledgement(
    db: Session,
    item: StaffAction,
    actor: str,
    status: str,
    note: str,
) -> None:
    if item.status != "approved":
        raise ValueError("Employee acknowledgement is available only after approval.")
    if status not in {"viewed", "declined"}:
        raise ValueError("Invalid acknowledgement status.")
    if status == "declined" and not note.strip():
        raise ValueError("Record a note when the employee declines to acknowledge.")
    old_status = item.acknowledgement_status
    item.acknowledgement_status = status
    item.acknowledgement_recorded_by = actor
    item.acknowledgement_at = utcnow()
    item.acknowledgement_note = note.strip() or None
    db.add(AuditLog(
        actor=actor,
        action="employee_action_acknowledgement_recorded",
        entity_type="staff_action",
        entity_id=str(item.id),
        old_value=old_status,
        new_value=status,
        reason=note.strip() or "HR recorded that the employee viewed the notice.",
    ))
