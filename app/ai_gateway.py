"""Approval gateway for the local HR AI command agent.

The model may prepare a plan, but every state-changing command must pass through
this gateway. Approval tokens are single-use, hashed at rest, time-limited, and
bound to the administrator who requested the plan.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.ai_agent import build_plan, execute_plan
from app.db import SessionLocal
from app.leave_workflow import leave_balance
from app.models import (
    AICommandApproval, AIConversationMessage, AdminAccount, AuditLog,
    Employee, Department, Branch, LeaveRecord, LeaveType, AdvanceRequest,
    StaffAction, AttendanceDailyRecord, AttendanceMonth, PayrollRun,
    LeaveBalanceTransaction,
)

APPROVAL_TTL_MINUTES = 10


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def plan_hash(plan: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(plan).encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _display_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def build_confirmation_preview(plan: dict[str, Any]) -> dict[str, Any]:
    """Read current database values and build an operation-specific before/after preview."""
    action = str(plan.get("action") or "")
    old: dict[str, Any] = {}
    new_values: dict[str, Any] = {}
    snapshot: dict[str, Any] = {"action": action, "target": None, "current": {}}
    risks: list[str] = []

    with SessionLocal() as db:
        if action in {"create_employee", "edit_employee", "transfer_employee", "archive_employee", "rehire_employee"}:
            employee_id = plan.get("employee_id")
            if employee_id is not None:
                employee = db.scalar(select(Employee).where(Employee.id == int(employee_id)).with_for_update())
                if employee is None:
                    raise ValueError("الموظف المستهدف غير موجود عند تجهيز المعاينة.")
                fields = ("code", "full_name", "status", "department_id", "branch_id", "position",
                          "hire_date", "basic_salary", "daily_hours", "phone", "address",
                          "national_id", "fingerprint_id")
                old = {key: _display_value(getattr(employee, key)) for key in fields}
                snapshot = {"action": action, "target": {"type": "employee", "id": employee.id},
                            "current": old}
            if action == "create_employee":
                keys = ("full_name", "position", "department_id", "branch_id", "hire_date",
                        "basic_salary", "daily_hours", "phone", "address", "national_id", "fingerprint_id")
                new_values = {key: _display_value(plan[key]) for key in keys if key in plan}
                risks.append("سيتم إنشاء سجل وظيفي جديد وتخصيص كود موظف لا يُعاد استخدامه.")
            elif action == "edit_employee":
                keys = ("full_name", "position", "department_id", "branch_id", "basic_salary",
                        "daily_hours", "phone", "address", "national_id", "fingerprint_id", "effective_date")
                new_values = {key: _display_value(plan[key]) for key in keys if key in plan}
                if "basic_salary" in plan or "daily_hours" in plan:
                    risks.append("تغيير الراتب أو ساعات العمل يؤثر على احتساب الرواتب حسب تاريخ السريان.")
            elif action == "transfer_employee":
                new_values = {key: _display_value(plan.get(key)) for key in ("department_id", "branch_id", "effective_date")}
                risks.append("سيُحفظ النقل في سجل التغييرات بتاريخ السريان المحدد.")
            elif action == "archive_employee":
                new_values = {"status": "archived", "service_period": "إغلاق فترة الخدمة المفتوحة"}
                risks.append("الأرشفة لا تحذف التاريخ الوظيفي أو المالي.")
            elif action == "rehire_employee":
                new_values = {"status": "active", "hire_date": _display_value(plan.get("hire_date"))}
                risks.append("إعادة التعيين ستنشئ فترة خدمة جديدة مع الاحتفاظ بكود الموظف السابق.")

        elif action in {"create_leave", "confirm_leave", "cancel_leave", "delete_draft_leave"}:
            record_id = plan.get("record_id")
            if record_id is not None:
                record = db.scalar(select(LeaveRecord).where(LeaveRecord.id == int(record_id)).with_for_update())
                if record is None:
                    raise ValueError("سجل الإجازة المستهدف غير موجود.")
                old = {"status": record.status, "employee_id": record.employee_id,
                       "leave_type_id": record.leave_type_id, "start_date": record.start_date.isoformat(),
                       "end_date": record.end_date.isoformat(), "charged_days": str(record.charged_days)}
                snapshot = {"action": action, "target": {"type": "leave", "id": record.id}, "current": old}
            if action == "create_leave":
                new_values = {key: _display_value(plan.get(key)) for key in
                              ("employee_id", "leave_type_id", "start_date", "end_date", "charged_days", "reason")}
                risks.append("سيتم إنشاء مسودة إجازة؛ لا يُخصم الرصيد إلا عند اعتمادها.")
            elif action == "confirm_leave":
                new_values = {"status": "confirmed", "balance_effect": "خصم الرصيد إذا كان نوع الإجازة يتتبع رصيدًا"}
                risks.append("اعتماد الإجازة قد يخصم أيامًا من رصيد الموظف.")
            elif action == "cancel_leave":
                new_values = {"status": "cancelled", "balance_effect": "إعادة الرصيد إذا كانت الإجازة مؤكدة"}
                risks.append("إلغاء إجازة مؤكدة ينشئ حركة عكسية في دفتر الرصيد ولا يمحو الحركة الأصلية.")
            else:
                new_values = {"status": "deleted"}
                risks.append("الحذف متاح لمسودة غير معتمدة فقط، بشرط عدم وجود حركة رصيد مرتبطة. سيظل سجل التدقيق محفوظًا.")

        elif action == "adjust_leave_balance":
            employee_id = int(plan.get("employee_id") or 0)
            leave_type_id = int(plan.get("leave_type_id") or 0)
            employee = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
            leave_type = db.scalar(select(LeaveType).where(LeaveType.id == leave_type_id).with_for_update())
            if employee is None or leave_type is None:
                raise ValueError("الموظف أو نوع الإجازة المستهدف غير موجود.")
            if not leave_type.tracks_balance:
                raise ValueError("نوع الإجازة المحدد لا يستخدم رصيدًا.")
            current_balance = leave_balance(db, employee_id, leave_type_id)
            delta = Decimal(str(plan.get("balance_days") or "0")).quantize(Decimal("0.01"))
            transaction_type = str(plan.get("transaction_type") or "adjustment")
            opening_exists = db.scalar(select(LeaveBalanceTransaction.id).where(
                LeaveBalanceTransaction.employee_id == employee_id,
                LeaveBalanceTransaction.leave_type_id == leave_type_id,
                LeaveBalanceTransaction.transaction_type == "opening",
            ).limit(1)) is not None
            resulting_balance = current_balance + delta
            if delta == 0:
                raise ValueError("تعديل الرصيد يجب أن يكون بقيمة غير صفرية.")
            if transaction_type == "opening" and opening_exists:
                raise ValueError("تم تسجيل الرصيد الافتتاحي من قبل؛ استخدم تعديلًا بدلًا منه.")
            if transaction_type == "adjustment" and resulting_balance < 0:
                raise ValueError("التعديل المقترح سيجعل رصيد الإجازة سالبًا.")
            old = {
                "employee_code": employee.code,
                "employee_name": employee.full_name,
                "leave_type": leave_type.name_ar,
                "current_balance": str(current_balance),
                "opening_balance_exists": opening_exists,
            }
            new_values = {
                "transaction_type": transaction_type,
                "days_delta": str(delta),
                "resulting_balance": str(resulting_balance),
                "reason": _display_value(plan.get("reason")),
            }
            snapshot = {
                "action": action,
                "target": {"type": "leave_balance", "employee_id": employee_id, "leave_type_id": leave_type_id},
                "current": old,
            }
            risks.append("سيُسجل التعديل كحركة رصيد دائمة في دفتر الإجازات، ولا تُمحى الحركات السابقة.")

        elif action in {"create_advance", "approve_advance", "reject_advance", "mark_advance_paid"}:
            advance_id = plan.get("advance_id")
            if advance_id is not None:
                item = db.scalar(select(AdvanceRequest).where(AdvanceRequest.id == int(advance_id)).with_for_update())
                if item is None:
                    raise ValueError("طلب السلفة المستهدف غير موجود.")
                old = {"status": item.status, "employee_id": item.employee_id, "amount": str(item.amount),
                       "advance_type": item.advance_type, "payroll_month": item.payroll_month.isoformat()}
                snapshot = {"action": action, "target": {"type": "advance", "id": item.id}, "current": old}
            if action == "create_advance":
                new_values = {key: _display_value(plan.get(key)) for key in
                              ("employee_id", "amount", "advance_type", "request_date", "installment_count")}
                risks.append("السلفة العادية تخضع لحد 25% ونافذة 15–22؛ سلفة التقسيط مستقلة وتحتاج جدولًا من 1 إلى 36 قسطًا.")
            elif action == "approve_advance":
                new_values = {"status": "approved", "installment_count": _display_value(plan.get("installment_count"))}
                risks.append("الاعتماد ينشئ جدول الأقساط أو يحجز حد السلفة وفق نوعها.")
            elif action == "reject_advance":
                new_values = {"status": "rejected"}
            else:
                new_values = {"status": "paid"}
                risks.append("تسجيل السداد يؤثر على دفتر الأقساط ومسير الرواتب.")

        elif action in {"create_staff_action", "approve_staff_action", "cancel_staff_action"}:
            item_id = plan.get("staff_action_id")
            if item_id is not None:
                item = db.scalar(select(StaffAction).where(StaffAction.id == int(item_id)).with_for_update())
                if item is None:
                    raise ValueError("إجراء الموظف المستهدف غير موجود.")
                old = {"status": item.status, "employee_id": item.employee_id, "kind": item.kind,
                       "category": item.category, "amount": _display_value(item.amount),
                       "payroll_month": _display_value(item.payroll_month)}
                snapshot = {"action": action, "target": {"type": "staff_action", "id": item.id}, "current": old}
            if action == "create_staff_action":
                new_values = {key: _display_value(plan.get(key)) for key in
                              ("employee_id", "kind", "category", "title", "is_financial", "amount", "payroll_month")}
                risks.append("المبالغ لا تُنشأ تلقائيًا؛ القيمة المدخلة يدويًا ستدخل الرواتب بعد الاعتماد.")
            elif action == "approve_staff_action":
                new_values = {"status": "approved"}
                risks.append("إذا كان الإجراء ماليًا فسيدخل في مسير شهر الراتب المحدد.")
            else:
                new_values = {"status": "cancelled"}
                risks.append("لا يمكن إلغاء إجراء محجوز لمسير رواتب أو مطبق عليه.")

        elif action == "create_manual_attendance":
            employee_id = int(plan.get("employee_id") or 0)
            employee = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
            if employee is None:
                raise ValueError("الموظف المستهدف غير موجود.")
            work_date = date.fromisoformat(str(plan["work_date"]))
            month = db.scalar(select(AttendanceMonth).where(
                AttendanceMonth.period_start <= work_date,
                AttendanceMonth.period_end >= work_date,
            ).with_for_update())
            existing = db.scalar(select(AttendanceDailyRecord).where(
                AttendanceDailyRecord.employee_id == employee_id,
                AttendanceDailyRecord.work_date == work_date,
            ).with_for_update())
            old = {
                "employee": employee.full_name,
                "employee_status": employee.status,
                "attendance_month_status": month.status if month else None,
                "existing_record_status": existing.status if existing else "no_record",
            }
            new_values = {
                "employee_id": employee_id,
                "work_date": work_date.isoformat(),
                "clock_in": _display_value(plan.get("clock_in")),
                "clock_out": _display_value(plan.get("clock_out")),
                "status": "needs_review",
                "reason": _display_value(plan.get("reason")),
            }
            snapshot = {"action": action, "target": {"type": "manual_attendance", "employee_id": employee_id,
                        "work_date": work_date.isoformat()}, "current": old}
            risks.append("سيُسجل الحضور كإدخال HR يدوي مستقل عن البصمة، وسيظل بحاجة إلى المراجعة والاعتماد.")

        elif action == "approve_attendance_day":
            record_id = plan.get("attendance_record_id")
            item = db.scalar(select(AttendanceDailyRecord).where(
                AttendanceDailyRecord.id == int(record_id)).with_for_update()) if record_id is not None else None
            if item is None:
                raise ValueError("سجل الحضور المستهدف غير موجود.")
            old = {"status": item.status, "employee_id": item.employee_id,
                   "work_date": item.work_date.isoformat(),
                   "clock_in": _display_value(item.first_clock_in),
                   "clock_out": _display_value(item.last_clock_out)}
            new_values = {"status": "approved", "clock_in": _display_value(plan.get("clock_in")),
                          "clock_out": _display_value(plan.get("clock_out"))}
            snapshot = {"action": action, "target": {"type": "attendance", "id": item.id}, "current": old}
            risks.append("اعتماد الحضور يؤثر على حساب التأخير والغياب والرواتب.")

        elif action == "approve_attendance_month":
            month_id = plan.get("month_id")
            item = db.scalar(select(AttendanceMonth).where(
                AttendanceMonth.id == int(month_id)).with_for_update()) if month_id is not None else None
            if item is None:
                raise ValueError("شهر الحضور المستهدف غير موجود.")
            old = {"status": item.status, "period_start": item.period_start.isoformat(),
                   "period_end": item.period_end.isoformat()}
            new_values = {"status": "approved", "coverage_confirmed": bool(plan.get("coverage_confirmed"))}
            snapshot = {"action": action, "target": {"type": "attendance_month", "id": item.id}, "current": old}
            risks.append("اعتماد الشهر شرط سابق لإنشاء واعتماد مسير الرواتب.")

        elif action in {"approve_payroll_run", "close_payroll_run", "reopen_payroll_run"}:
            run_id = plan.get("payroll_run_id")
            item = db.scalar(select(PayrollRun).where(
                PayrollRun.id == int(run_id)).with_for_update()) if run_id is not None else None
            if item is None:
                raise ValueError("مسير الرواتب المستهدف غير موجود.")
            old = {"status": item.status, "period_start": item.period_start.isoformat(),
                   "period_end": item.period_end.isoformat()}
            target_status = {"approve_payroll_run": "approved", "close_payroll_run": "closed",
                             "reopen_payroll_run": "reopened"}[action]
            new_values = {"status": target_status, "reason": _display_value(plan.get("reason"))}
            snapshot = {"action": action, "target": {"type": "payroll_run", "id": item.id}, "current": old}
            risks.append("هذه عملية مالية مؤثرة. راجع كل بنود المسير والاستقطاعات والأقساط قبل التأكيد.")

        else:
            new_values = {key: _display_value(value) for key, value in plan.items()
                          if key not in {"_confirmation_snapshot", "action", "summary"}}
            risks.append("العملية تحتاج مراجعة إضافية قبل اعتمادها.")

    return {"old_values": old, "new_values": new_values, "risks": risks, "snapshot": snapshot}


def _as_utc(value: datetime) -> datetime:
    """Normalize database timestamps from SQLite/PostgreSQL to aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _memory(actor: str, role: str, content: str) -> None:
    with SessionLocal.begin() as db:
        db.add(AIConversationMessage(actor=actor, role=role, content=content[:10000]))


def record_exchange(actor: str, question: str, answer: str) -> None:
    """Persist an ordinary Q&A exchange so the conversation thread is real,
    not just command history. Stored in the language it was rendered in."""
    _memory(actor, "user", question)
    _memory(actor, "assistant", answer)


def recent_memory(actor: str, limit: int = 12) -> list[dict[str, str]]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(AIConversationMessage)
            .where(AIConversationMessage.actor == actor)
            .order_by(AIConversationMessage.id.desc())
            .limit(limit)
        ).all()
    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows]


def prepare_command(question: str, actor: str) -> dict[str, Any]:
    history = recent_memory(actor, limit=8)
    history_text = "\n".join(f"{item['role']}: {item['content']}" for item in history)
    planning_question = question
    if history_text:
        planning_question = f"السياق السابق للمحادثة (للفهم فقط):\\n{history_text}\\n\\nالأمر الحالي: {question}"
    plan = build_plan(planning_question)
    preview = build_confirmation_preview(plan)
    plan["_confirmation_snapshot"] = preview["snapshot"]
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=APPROVAL_TTL_MINUTES)
    summary = str(plan.get("summary") or "تم إعداد خطة للتنفيذ بعد الموافقة.")
    with SessionLocal.begin() as db:
        db.add(AICommandApproval(
            token_hash=_token_hash(token),
            actor=actor,
            question=question,
            plan_json=_canonical_json(plan),
            plan_hash=plan_hash(plan),
            status="pending",
            created_at=now,
            expires_at=expires,
        ))
        db.add(AuditLog(
            actor=actor,
            action="ai_command_proposed",
            entity_type="ai_command",
            entity_id=_token_hash(token),
            new_value=f"action={plan.get('action')}; plan_hash={plan_hash(plan)}",
            reason=question,
        ))
    _memory(actor, "user", question)
    _memory(actor, "assistant", f"خطة معلقة تحتاج موافقة: {summary}")
    return {
        "token": token,
        "plan": {key: value for key, value in plan.items() if key != "_confirmation_snapshot"},
        "summary": summary,
        "old_values": preview["old_values"],
        "new_values": preview["new_values"],
        "risks": preview["risks"],
        "expires_at": expires,
    }


def reject_command(token: str, actor: str, reason: str = "رفض المسؤول العملية.") -> str:
    now = datetime.now(timezone.utc)
    expired = False
    with SessionLocal.begin() as db:
        approval = db.scalar(
            select(AICommandApproval)
            .where(AICommandApproval.token_hash == _token_hash(token))
            .with_for_update()
        )
        if approval is None or approval.actor != actor:
            raise ValueError("طلب الموافقة غير موجود.")
        if approval.status != "pending":
            raise ValueError("هذا الطلب لم يعد قابلًا للرفض.")
        if _as_utc(approval.expires_at) <= now:
            approval.status = "expired"
            expired = True
            db.add(AuditLog(
                actor=actor, action="ai_command_expired", entity_type="ai_command",
                entity_id=str(approval.id), reason="انتهت صلاحية طلب الموافقة.",
            ))
        else:
            approval.status = "rejected"
            approval.rejected_at = now
            db.add(AuditLog(
                actor=actor, action="ai_command_rejected", entity_type="ai_command",
                entity_id=str(approval.id), reason=reason,
            ))
    # Raise only after the transaction commits, or the terminal state would roll back.
    if expired:
        raise ValueError("انتهت صلاحية طلب الموافقة.")
    _memory(actor, "assistant", "تم رفض العملية ولم يتم تغيير بيانات النظام.")
    return "تم رفض العملية. لم يتم تغيير أي بيانات."


def approve_command(token: str, actor: str) -> str:
    now = datetime.now(timezone.utc)
    plan: dict[str, Any] | None = None
    terminal_error: str | None = None
    with SessionLocal.begin() as db:
        approval = db.scalar(
            select(AICommandApproval)
            .where(AICommandApproval.token_hash == _token_hash(token))
            .with_for_update()
        )
        if approval is None or approval.actor != actor:
            raise ValueError("طلب الموافقة غير موجود.")
        if approval.status != "pending":
            raise ValueError("هذا الطلب سبق التعامل معه ولا يمكن تنفيذه مرة أخرى.")
        if _as_utc(approval.expires_at) <= now:
            approval.status = "expired"
            db.add(AuditLog(
                actor=actor, action="ai_command_expired", entity_type="ai_command",
                entity_id=str(approval.id), reason="انتهت صلاحية طلب الموافقة.",
            ))
            terminal_error = "انتهت صلاحية طلب الموافقة."
        else:
            try:
                candidate = json.loads(approval.plan_json)
                if not isinstance(candidate, dict):
                    raise ValueError("stored plan is not a JSON object")
                candidate_hash = plan_hash(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                candidate = None
                candidate_hash = None
            if candidate is None or candidate_hash != approval.plan_hash:
                approval.status = "invalidated"
                db.add(AuditLog(
                    actor=actor, action="ai_command_invalidated", entity_type="ai_command",
                    entity_id=str(approval.id),
                    reason="خطة التنفيذ المخزنة غير صالحة أو لم تعد مطابقة للخطة المعروضة.",
                ))
                terminal_error = "خطة التنفيذ لم تعد مطابقة للخطة التي تم عرضها."
            else:
                expected_snapshot = candidate.get("_confirmation_snapshot")
                try:
                    current_preview = build_confirmation_preview(candidate)
                except ValueError:
                    current_preview = None
                if expected_snapshot is not None and (
                    current_preview is None or current_preview["snapshot"] != expected_snapshot
                ):
                    approval.status = "invalidated"
                    db.add(AuditLog(
                        actor=actor, action="ai_command_invalidated", entity_type="ai_command",
                        entity_id=str(approval.id),
                        reason="تغيرت بيانات السجل بعد عرض المعاينة وقبل التأكيد.",
                    ))
                    terminal_error = "تغيرت بيانات السجل منذ عرض المعاينة. جهّز الأمر مرة أخرى وراجع القيم الجديدة."
                else:
                    plan = candidate
                    approval.status = "executing"
                    approval.approved_at = now
                    db.add(AuditLog(
                        actor=actor, action="ai_command_approved", entity_type="ai_command",
                        entity_id=str(approval.id), new_value=f"plan_hash={approval.plan_hash}",
                        reason="موافقة صريحة من مسؤول الموارد البشرية.",
                    ))
    # Persist expired/invalidated states before surfacing the corresponding error.
    if terminal_error is not None:
        raise ValueError(terminal_error)
    if plan is None:
        raise RuntimeError("لم يتم تجهيز خطة تنفيذ صالحة بعد الموافقة.")
    try:
        message = execute_plan(plan, actor)
    except Exception as exc:
        with SessionLocal.begin() as db:
            approval = db.scalar(
                select(AICommandApproval)
                .where(AICommandApproval.token_hash == _token_hash(token))
                .with_for_update()
            )
            if approval is not None and approval.status == "executing":
                approval.status = "failed"
                approval.failure_reason = str(exc)[:2000]
                db.add(AuditLog(
                    actor=actor, action="ai_command_failed", entity_type="ai_command",
                    entity_id=str(approval.id), reason=str(exc)[:2000],
                ))
        raise
    with SessionLocal.begin() as db:
        approval = db.scalar(
            select(AICommandApproval)
            .where(AICommandApproval.token_hash == _token_hash(token))
            .with_for_update()
        )
        if approval is None or approval.status != "executing":
            raise RuntimeError("حالة الموافقة غير صالحة بعد التنفيذ.")
        approval.status = "consumed"
        approval.consumed_at = datetime.now(timezone.utc)
        db.add(AuditLog(
            actor=actor, action="ai_command_executed", entity_type="ai_command",
            entity_id=str(approval.id), new_value=f"plan_hash={approval.plan_hash}",
            reason=message,
        ))
    _memory(actor, "assistant", message)
    return message
