"""Approval gateway for the local HR AI command agent.

The model may prepare a plan, but every state-changing command must pass through
this gateway. Approval tokens are single-use, hashed at rest, time-limited, and
bound to the administrator who requested the plan.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text

from app.ai_agent import build_plan, execute_plan
from app.ai_errors import LocalizedValueError
from app.ai_execution_render import render_execution_result
from app.ai_plan_validation import validate_plan_values
from app.db import SessionLocal
from app.leave_balance_rules import preview_manual_balance
from app.leave_workflow import leave_balance
from app.models import (
    AdvanceRequest,
    AICommandApproval,
    AIConversationMessage,
    AttendanceDailyRecord,
    AttendanceMonth,
    AuditLog,
    Employee,
    LeaveBalanceTransaction,
    LeaveRecord,
    LeaveType,
    PayrollItem,
    PayrollPayment,
    PayrollRun,
    StaffAction,
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


def build_confirmation_preview(plan: dict[str, Any], *, session=None) -> dict[str, Any]:
    """Read current database values and build an operation-specific before/after preview."""
    validate_plan_values(plan)
    action = str(plan.get("action") or "")
    old: dict[str, Any] = {}
    new_values: dict[str, Any] = {}
    snapshot: dict[str, Any] = {"action": action, "target": None, "current": {}}
    risks: list[str] = []

    with (SessionLocal() if session is None else nullcontext(session)) as db:
        if session is None:
            _lock_command_business_tables(db)
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
                if isinstance(new_values.get("fingerprint_id"), str):
                    new_values["fingerprint_id"] = new_values["fingerprint_id"].strip() or None
                risks.append("سيتم إنشاء سجل وظيفي جديد وتخصيص كود موظف لا يُعاد استخدامه.")
            elif action == "edit_employee":
                keys = ("full_name", "position", "department_id", "branch_id", "basic_salary",
                        "daily_hours", "phone", "address", "national_id", "fingerprint_id", "effective_date")
                # execute_plan skips null fields (unchanged), so the preview must not show them as changes.
                new_values = {key: _display_value(plan[key]) for key in keys if plan.get(key) is not None}
                if isinstance(new_values.get("fingerprint_id"), str):
                    new_values["fingerprint_id"] = new_values["fingerprint_id"].strip()
                if plan.get("basic_salary") is not None or plan.get("daily_hours") is not None:
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
            delta = Decimal(str(plan.get("balance_days") or "0"))
            transaction_type = str(plan.get("transaction_type") or "adjustment")
            opening_exists = db.scalar(select(LeaveBalanceTransaction.id).where(
                LeaveBalanceTransaction.employee_id == employee_id,
                LeaveBalanceTransaction.leave_type_id == leave_type_id,
                LeaveBalanceTransaction.transaction_type == "opening",
            ).limit(1)) is not None
            resulting_balance = preview_manual_balance(current_balance, transaction_type, delta)
            if transaction_type == "opening" and opening_exists:
                raise ValueError("تم تسجيل الرصيد الافتتاحي من قبل؛ استخدم تعديلًا بدلًا منه.")
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
            attendance_source = {}
            for table in sorted(Employee.metadata.sorted_tables, key=lambda t: t.name):
                if (table.name.startswith(("attendance_", "fingerprint_", "program_"))
                        or table.name in {"employees", "employee_service_periods", "shift_patterns",
                                          "leave_records", "leave_types"}):
                    rows = db.execute(select(table).order_by(*table.primary_key.columns)).mappings().all()
                    attendance_source[table.name] = [{key: _display_value(value) for key, value in row.items()}
                                                     for row in rows]
            old["source_digest"] = hashlib.sha256(_canonical_json(attendance_source).encode()).hexdigest()
            new_values = {"status": "approved", "coverage_confirmed": False}
            snapshot = {"action": action, "target": {"type": "attendance_month", "id": item.id}, "current": old}
            risks.append("اعتماد الشهر شرط سابق لإنشاء واعتماد مسير الرواتب.")

        elif action in {"approve_payroll_run", "close_payroll_run", "reopen_payroll_run"}:
            from app.overtime_day_award import monthly_lock_scope
            monthly_lock_scope(db)
            run_id = plan.get("payroll_run_id")
            item = db.scalar(select(PayrollRun).where(
                PayrollRun.id == int(run_id)).with_for_update()) if run_id is not None else None
            if item is None:
                raise ValueError("مسير الرواتب المستهدف غير موجود.")
            old = {"status": item.status, "period_start": item.period_start.isoformat(),
                   "period_end": item.period_end.isoformat()}
            # Bind every stored financial input, output and review flag, not
            # merely run status. String encoding preserves Decimal precision.
            items = db.scalars(select(PayrollItem).where(
                PayrollItem.payroll_run_id == item.id).order_by(PayrollItem.id).with_for_update()).all()
            old["payroll_items"] = [{column.name: _display_value(getattr(row, column.name))
                for column in PayrollItem.__table__.columns} for row in items]
            payments = db.scalars(select(PayrollPayment).join(PayrollItem,
                PayrollPayment.payroll_item_id == PayrollItem.id).where(
                PayrollItem.payroll_run_id == item.id).order_by(PayrollPayment.id)).all()
            old["payments"] = [{column.name: _display_value(getattr(row, column.name))
                for column in PayrollPayment.__table__.columns} for row in payments]
            # Source facts whose edits can invalidate absence/advance/staff
            # review must also invalidate the financial confirmation. Broad
            # source coverage deliberately prefers false-stale over omission.
            source_names = {"leave_records", "leave_types", "attendance_months",
                "attendance_daily_records", "advance_requests", "advance_installments",
                "staff_actions", "employees", "employee_service_periods", "employee_changes",
                "overtime_day_awards", "overtime_day_review_receipts"}
            source_payload = {}
            for table in sorted(Employee.metadata.sorted_tables, key=lambda t: t.name):
                if table.name in source_names:
                    rows = db.execute(select(table).order_by(*table.primary_key.columns)).mappings().all()
                    source_payload[table.name] = [{key: _display_value(value) for key, value in row.items()}
                                                  for row in rows]
            old["source_digest"] = hashlib.sha256(_canonical_json(source_payload).encode()).hexdigest()
            from app.payroll_workflow import recompute_payroll_item
            projected = []
            for row in items:
                clone = PayrollItem(**{column.name: getattr(row, column.name)
                                       for column in PayrollItem.__table__.columns})
                recompute_payroll_item(clone, item)
                projected.append({"id": row.id, "employee_code": row.employee_code_snapshot,
                                  "employee_name": row.employee_name_snapshot, "net_pay": str(clone.net_pay),
                                  "overtime_amount": str(clone.overtime_amount),
                                  "absence_deduction": str(clone.absence_deduction)})
            old["calculated_items"] = projected
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
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _msg(language: str, ar: str, en: str, zh: str) -> str:
    return {"ar": ar, "en": en, "zh": zh}.get(language, ar)


def _memory(actor: str, role: str, content: str, language: str = "unknown") -> None:
    """Append one entry. History is never auto-deleted: there is no user
    authority for irreversible cleanup, so only reads are bounded."""
    with SessionLocal.begin() as db:
        db.add(AIConversationMessage(
            actor=actor, role=role, content=content[:10000], language=language,
        ))


def record_exchange(actor: str, question: str, answer: str, language: str = "ar") -> None:
    """Persist an ordinary Q&A exchange so the conversation thread is real,
    not just command history. The assistant answer's language is the locale it
    was generated in (grounded, known). The user's typed question keeps
    language "unknown": the UI locale is not the authored language and script
    heuristics mislabel, so user text honestly carries no origin badge."""
    _memory(actor, "user", question, "unknown")
    _memory(actor, "assistant", answer, language)


def recent_memory(actor: str, limit: int = 12) -> list[dict[str, str]]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(AIConversationMessage)
            .where(AIConversationMessage.actor == actor)
            .order_by(AIConversationMessage.id.desc())
            .limit(limit)
        ).all()
    rows.reverse()
    return [
        {"role": row.role, "content": row.content, "language": row.language or "unknown"}
        for row in rows
    ]


def prepare_command(question: str, actor: str, language: str = "ar") -> dict[str, Any]:
    history = recent_memory(actor, limit=8)
    history_text = "\n".join(f"{item['role']}: {item['content']}" for item in history)
    planning_question = question
    if history_text:
        planning_question = f"السياق السابق للمحادثة (للفهم فقط):\\n{history_text}\\n\\nالأمر الحالي: {question}"
    plan = build_plan(planning_question, language=language)
    preview = build_confirmation_preview(plan)
    plan["_confirmation_snapshot"] = preview["snapshot"]
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=APPROVAL_TTL_MINUTES)
    summary = str(plan.get("summary") or _msg(
        language,
        "تم إعداد خطة للتنفيذ بعد الموافقة.",
        "A plan has been prepared and will run after your approval.",
        "已准备好计划，将在您批准后执行。",
    ))
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
    _memory(actor, "user", question, "unknown")
    _memory(actor, "assistant", _msg(
        language,
        "خطة معلقة تحتاج موافقة: ",
        "Pending plan awaiting your approval: ",
        "待批准的计划，等待您确认：",
    ) + summary, language)
    return {
        "token": token,
        "plan": {key: value for key, value in plan.items() if key != "_confirmation_snapshot"},
        "summary": summary,
        "old_values": preview["old_values"],
        "new_values": preview["new_values"],
        "risks": preview["risks"],
        "expires_at": expires,
    }


def reject_command(
    token: str,
    actor: str,
    reason: str = "رفض المسؤول العملية.",
    language: str = "ar",
) -> str:
    now = datetime.now(UTC)
    expired = False
    with SessionLocal.begin() as db:
        approval = db.scalar(
            select(AICommandApproval)
            .where(AICommandApproval.token_hash == _token_hash(token))
            .with_for_update()
        )
        if approval is None or approval.actor != actor:
            raise LocalizedValueError(_msg(
                language, "طلب الموافقة غير موجود.",
                "Approval request not found.", "未找到该批准请求。",
            ), language)
        if approval.status != "pending":
            raise LocalizedValueError(_msg(
                language, "هذا الطلب لم يعد قابلًا للرفض.",
                "This request can no longer be rejected.", "该请求已无法拒绝。",
            ), language)
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
        raise LocalizedValueError(_msg(
            language, "انتهت صلاحية طلب الموافقة.",
            "The approval request has expired.", "批准请求已过期。",
        ), language)
    _memory(actor, "assistant", _msg(
        language,
        "تم رفض العملية ولم يتم تغيير بيانات النظام.",
        "The operation was rejected; system data was not changed.",
        "操作已拒绝，系统数据未更改。",
    ), language)
    return _msg(
        language,
        "تم رفض العملية. لم يتم تغيير أي بيانات.",
        "The operation was rejected. No data was changed.",
        "操作已拒绝，未更改任何数据。",
    )


def _lock_command_business_tables(db):
    """Serialize deterministic execution with ALL ordinary business writers.

    Never held while waiting for model output or human approval. PostgreSQL
    SHARE ROW EXCLUSIVE conflicts with DML, including writers that do not take
    our application row locks. A short timeout fails closed, never retries a
    partly committed mutation. Table order is stable across AI executions.
    """
    if db.bind.dialect.name == "postgresql":
        # Same global order as daily OT: advisory gate before business tables.
        # Taking business tables first can deadlock a daily writer holding the
        # exclusive advisory gate while waiting for one of these tables.
        from app.restore_lock import acquire_authenticated_read_lock
        acquire_authenticated_read_lock(db)
        db.execute(text("SET LOCAL lock_timeout = '3s'"))
        names = sorted(table.name for table in Employee.metadata.sorted_tables
                       if table.name not in {"ai_command_approvals", "ai_conversation_messages"})
        db.execute(text("LOCK TABLE " + ", ".join('"' + name + '"' for name in names)
                        + " IN SHARE ROW EXCLUSIVE MODE"))


def approve_command(token: str, actor: str, language: str = "ar", *,
                    human_coverage_confirmed: bool = False) -> str:
    if type(human_coverage_confirmed) is not bool:
        raise LocalizedValueError(_msg(language, "يلزم تأكيد صريح من مسؤول الموارد البشرية.", "Explicit human HR confirmation required.", "需要人力资源人员明确确认。"), language)
    now = datetime.now(UTC)
    terminal_error = None
    result = None
    rendered = None
    with SessionLocal.begin() as db:
        # Acquire table locks before token row locks, consistently across calls.
        _lock_command_business_tables(db)
        now = datetime.now(UTC)
        approval = db.scalar(select(AICommandApproval).where(
            AICommandApproval.token_hash == _token_hash(token)).with_for_update())
        if approval is None or approval.actor != actor:
            raise LocalizedValueError(_msg(language, "طلب الموافقة غير موجود.",
                                  "Approval request not found.", "未找到该批准请求。"), language)
        if approval.status != "pending":
            raise LocalizedValueError(_msg(language,
                "هذا الطلب سبق التعامل معه ولا يمكن تنفيذه مرة أخرى.",
                "This request was already handled and cannot be executed again.",
                "该请求已处理，无法再次执行。"), language)
        if _as_utc(approval.expires_at) <= now:
            approval.status = "expired"
            terminal_error = _msg(language, "انتهت صلاحية طلب الموافقة.",
                                  "The approval request has expired.", "批准请求已过期。")
            db.add(AuditLog(actor=actor, action="ai_command_expired", entity_type="ai_command",
                           entity_id=str(approval.id), reason=terminal_error))
        else:
            try:
                plan = json.loads(approval.plan_json)
                validate_plan_values(plan, language)
                valid_hash = plan_hash(plan) == approval.plan_hash
            except (ValueError, TypeError, json.JSONDecodeError):
                plan, valid_hash = None, False
            expected = plan.get("_confirmation_snapshot") if plan else None
            if not valid_hash or expected is None:
                approval.status = "invalidated"
                terminal_error = _msg(language,
                    "خطة التنفيذ لم تعد مطابقة للخطة التي تم عرضها.",
                    "The stored plan no longer matches the one that was shown.",
                    "存储的执行计划与已展示的计划不一致。")
            else:
                try:
                    current = build_confirmation_preview(plan, session=db)
                except ValueError:
                    current = None
                if current is None or current["snapshot"] != expected:
                    approval.status = "invalidated"
                    terminal_error = _msg(language,
                        "تغيرت بيانات السجل منذ عرض المعاينة. جهّز الأمر مرة أخرى وراجع القيم الجديدة.",
                        "The record changed since the preview was shown. Prepare the command again and review the new values.",
                        "自预览以来记录已变更。请重新准备指令并核对新值。")
                else:
                    # Any domain failure rolls back this entire transaction;
                    # process crash rolls back business effects and token.
                    # There is never a separately committed executing state.
                    if plan.get("action") == "approve_attendance_month" and not human_coverage_confirmed:
                        raise LocalizedValueError(_msg(language, "يلزم تأكيدك الشخصي لمراجعة تغطية الحضور.", "Your explicit attendance coverage confirmation is required.", "需要您明确确认已审核出勤覆盖情况。"), language)
                    result = execute_plan(plan, actor, db=db,
                        human_coverage_confirmed=human_coverage_confirmed)
                    rendered = render_execution_result(result, language)
                    approval.status = "consumed"
                    approval.approved_at = now
                    approval.consumed_at = datetime.now(UTC)
                    db.add(AuditLog(actor=actor, action="ai_command_approved", entity_type="ai_command",
                        entity_id=str(approval.id), new_value=f"plan_hash={approval.plan_hash}",
                        reason="Explicit HR approval, atomic business transaction."))
                    db.add(AuditLog(actor=actor, action="ai_command_executed", entity_type="ai_command",
                        entity_id=str(approval.id), new_value=f"plan_hash={approval.plan_hash}", reason=result.audit))
                    # History is part of this commit, not a post-success step
                    # whose failure could misreport a committed operation.
                    db.add(AIConversationMessage(actor=actor, role="assistant",
                        content=result.audit[:10000], language="ar"))
            if terminal_error:
                db.add(AuditLog(actor=actor, action="ai_command_invalidated", entity_type="ai_command",
                    entity_id=str(approval.id), reason=terminal_error))
    if terminal_error:
        raise LocalizedValueError(terminal_error, language)
    return rendered


def review_command(token: str, actor: str, language: str = 'ar') -> dict:
    """Re-read a pending exact snapshot for the separate confirmation screen."""
    with SessionLocal.begin() as db:
        _lock_command_business_tables(db)
        record = db.scalar(select(AICommandApproval).where(
            AICommandApproval.token_hash == _token_hash(token)).with_for_update())
        if (record is None or record.actor != actor or record.status != 'pending'
                or _as_utc(record.expires_at) <= datetime.now(UTC)):
            raise LocalizedValueError(_msg(language, 'طلب الموافقة غير متاح أو انتهت صلاحيته.',
                                  'Approval is unavailable or expired.', '批准请求不可用或已过期。'), language)
        plan = json.loads(record.plan_json)
        if plan_hash(plan) != record.plan_hash:
            raise ValueError('Invalid plan.')
        preview = build_confirmation_preview(plan, session=db)
        if preview['snapshot'] != plan.get('_confirmation_snapshot'):
            raise LocalizedValueError(_msg(language, 'تغيرت البيانات. جهز معاينة جديدة.',
                                  'Data changed. Prepare a new preview.', '数据已变更。请重新生成预览。'), language)
        return {'token': token, 'plan': {key: value for key, value in plan.items()
                if key != '_confirmation_snapshot'}, 'summary': str(plan.get('summary') or ''),
                'old_values': preview['old_values'], 'new_values': preview['new_values'],
                'risks': preview['risks'], 'expires_at': record.expires_at,
                'reviewed': True, 'review_receipt': review_receipt(token, actor, record.plan_hash)}


def review_receipt(token: str, actor: str, digest: str) -> str:
    import hmac

    from app.config import settings
    return hmac.new(settings.session_secret.encode(),
        _canonical_json({'token': token, 'actor': actor, 'plan_hash': digest}).encode(),
        hashlib.sha256).hexdigest()


def verify_review_receipt(token: str, actor: str, receipt: str) -> bool:
    import hmac
    import re
    # Receipts are lowercase SHA-256 hex. Reject anything else (non-ASCII, wrong length) before the DB lookup.
    if type(receipt) is not str or re.fullmatch(r"[0-9a-f]{64}", receipt, re.ASCII) is None:
        return False
    with SessionLocal() as db:
        record = db.scalar(select(AICommandApproval).where(
            AICommandApproval.token_hash == _token_hash(token)))
        return bool(record and record.actor == actor and hmac.compare_digest(
            review_receipt(token, actor, record.plan_hash), receipt))
