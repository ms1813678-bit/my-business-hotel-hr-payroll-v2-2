"""What-if payroll simulation: hypothetical adjustments on a transient copy.

Everything here is read-only against the database and side-effect free: the
run's items are copied into detached objects, the hypothetical adjustment is
applied to the copies, and the system's own payroll engine
(payroll_workflow.recompute_payroll_item) recomputes them. Nothing is added
to the session, written, approved, or audited. The result is an advisory
comparison only.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PayrollItem, PayrollRun
from app.payroll_math import money
from app.payroll_workflow import recompute_payroll_item

# Only these item fields may be adjusted hypothetically. They are the same
# numeric inputs the payroll engine already consumes; no new business rules.
ADJUSTABLE_FIELDS = {
    "attendance_bonus",
    "performance_bonus",
    "occupancy_bonus",
    "other_bonus",
    "overtime_hours",
    "absence_days",
    "insurance_share",
    "taxes",
    "other_deductions",
    "advance_deduction",
    "lateness_deduction",
}

# Arabic display labels for the adjustable fields (same wording as the
# payroll item edit form, so existing catalog translations apply).
FIELD_LABELS = {
    "attendance_bonus": "مكافأة حضور",
    "performance_bonus": "مكافأة أداء",
    "occupancy_bonus": "مكافأة إشغال",
    "other_bonus": "مكافآت أخرى",
    "overtime_hours": "ساعات إضافي",
    "absence_days": "أيام غياب معتمدة",
    "lateness_deduction": "خصم تأخير/انصراف مبكر",
    "insurance_share": "حصة التأمين",
    "taxes": "الضرائب",
    "other_deductions": "خصومات أخرى",
    "advance_deduction": "خصم السلفة",
}


# Fields copied verbatim from the stored item into the transient simulation
# copy; everything the engine reads must be present.
_ITEM_FIELDS = [
    "employee_id",
    "employee_code_snapshot",
    "employee_name_snapshot",
    "department_name_snapshot",
    "position_snapshot",
    "salary_basis",
    "base_salary",
    "daily_hours",
    "attendance_bonus",
    "performance_bonus",
    "occupancy_bonus",
    "overtime_hours",
    "overtime_approved",
    "overtime_amount",
    "absence_days",
    "absence_deduction",
    "late_minutes",
    "early_minutes",
    "lateness_deduction",
    "insurance_share",
    "taxes",
    "other_deductions",
    "advance_deduction",
    "staff_reward_amount",
    "staff_sanction_amount",
    "other_bonus",
    "net_pay",
]


def _transient_copy(item: PayrollItem) -> PayrollItem:
    """Detach an item from the session: same inputs, never persisted."""
    copy = PayrollItem(payroll_run_id=item.payroll_run_id)
    for field in _ITEM_FIELDS:
        setattr(copy, field, getattr(item, field))
    return copy


def simulate_run_adjustment(
    db: Session,
    run_id: int,
    *,
    field: str,
    mode: str,
    value: Any,
    employee_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Compare the stored run with a hypothetical adjustment.

    field: one of ADJUSTABLE_FIELDS. mode: "add" (delta on top of the stored
    value) or "set" (absolute replacement). employee_ids: optional subset;
    when omitted the adjustment applies to every item in the run.

    Returns a dict with per-employee before/after rows, totals, and the
    stated assumptions. Items whose hypothetical deductions would exceed
    earnings are reported as infeasible (the engine refuses negative net),
    never silently clamped.
    """
    if field not in ADJUSTABLE_FIELDS:
        raise ValueError(f"field_not_adjustable:{field}")
    if mode not in {"add", "set"}:
        raise ValueError(f"mode_not_supported:{mode}")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("value_not_numeric") from exc
    # Same bounds as the real item edit form: non-negative, finite, sane size.
    # NaN/Infinity/huge exponents must never reach the engine (InvalidOperation).
    if not amount.is_finite():
        raise ValueError("value_not_finite")
    if amount < 0:
        raise ValueError("value_negative")
    if amount > Decimal("999999999.99"):
        raise ValueError("value_too_large")

    # Read-only invariant: NO ORM attribute access, query, lazy load or result
    # materialization may ever autoflush unrelated dirty session state. The
    # ENTIRE compare body runs under no_autoflush, and the return value is
    # built from plain strings/ints only, so nothing touching the ORM escapes
    # the block (deferred or expired attributes included).
    with db.no_autoflush:
        run = db.get(PayrollRun, run_id)
        if run is None:
            raise ValueError("run_not_found")
        items = db.scalars(
            select(PayrollItem).where(PayrollItem.payroll_run_id == run.id)
        ).all()
        rows: list[dict[str, Any]] = []
        total_before = Decimal("0.00")
        total_after = Decimal("0.00")
        infeasible = 0
        for item in items:
            if employee_ids is not None and item.employee_id not in employee_ids:
                continue
            baseline = _transient_copy(item)
            recompute_payroll_item(baseline, run)
            hypothetical = _transient_copy(item)
            current = Decimal(hypothetical.__getattribute__(field))
            hypothetical.__setattr__(
                field, money(current + amount) if mode == "add" else money(amount)
            )
            try:
                recompute_payroll_item(hypothetical, run)
                new_net = hypothetical.net_pay
                feasible = True
                reason = None
            except ValueError as exc:
                # Engine refuses: report the accurate reason, don't clamp.
                detail = str(exc)
                if detail.startswith("Payroll deductions exceed earnings"):
                    reason = "net_negative"
                elif detail.startswith("Invalid salary or absence days"):
                    reason = "absence_bounds"
                else:
                    reason = "engine"
                new_net = None
                feasible = False
                infeasible += 1
            rows.append(
                {
                    "employee_id": item.employee_id,
                    "employee_code": item.employee_code_snapshot,
                    "employee_name": item.employee_name_snapshot,
                    "field": field,
                    "stored_value": str(current),
                    "hypothetical_value": str(hypothetical.__getattribute__(field)),
                    "net_before": str(baseline.net_pay),
                    "net_after": str(new_net) if feasible else None,
                    "net_delta": str(money(new_net - baseline.net_pay)) if feasible else None,
                    "feasible": feasible,
                    "infeasible_reason": reason,
                }
            )
            total_before += baseline.net_pay
            if feasible:
                total_after += new_net

        # Aggregates are only meaningful when every compared item recomputed
        # successfully; with infeasible items a partial total would be misleading.
        complete = infeasible == 0
        return {
            "run_id": run.id,
            "run_status": run.status,
            "period_start": run.period_start.isoformat(),
            "field": field,
            "mode": mode,
            "value": str(amount),
            "scope": "selected_employees" if employee_ids is not None else "all_items",
            "items_compared": len(rows),
            "infeasible_items": infeasible,
            "totals_complete": complete,
            "total_net_before": str(money(total_before)),
            "total_net_after": str(money(total_after)) if complete else None,
            "total_net_delta": str(money(total_after - total_before)) if complete else None,
            "rows": rows,
        }
