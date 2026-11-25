"""Safe local command agent for the HR assistant.

The language model may propose an action, but it never gets direct database, shell,
filesystem, or network access. Only this fixed allowlist of business operations
can mutate the database, and every operation uses existing workflow rules.
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from app.advance_workflow import approve_advance, create_advance, mark_advance_paid, reject_advance
from app.ai_assistant import local_model_status
from app.ai_plan_validation import validate_plan_values
from app.attendance_workflow import (
    add_manual_attendance,
    approve_attendance_day,
    approve_attendance_month,
)
from app.db import SessionLocal
from app.employee_codes import allocate_employee_code
from app.leave_balance_rules import validate_manual_balance_change
from app.leave_workflow import LeaveDraftError, create_leave_draft, add_balance_entry, cancel_leave, confirm_leave, leave_balance
from app.models import (
    AdvanceRequest,
    AttendanceDailyRecord,
    AttendanceMonth,
    AuditLog,
    Branch,
    Department,
    Employee,
    EmployeeChange,
    EmployeeServicePeriod,
    LeaveBalanceTransaction,
    LeaveRecord,
    LeaveType,
    PayrollRun,
    StaffAction,
)
from app.national_id import ERROR_MESSAGE as NATIONAL_ID_ERROR
from app.national_id import is_valid_egyptian_national_id
from app.payroll_workflow import approve_payroll_run, close_payroll_run, reopen_payroll_run
from app.staff_actions_workflow import approve_staff_action, cancel_staff_action

WRITE_PREFIXES = ("نفذ ", "نفّذ ", "نفذ:", "نفّذ:", "执行 ", "执行：", "执行:")
# English execution prefixes are matched case-insensitively.
EN_WRITE_PREFIXES = ("execute ", "execute:")
COMMAND_WORDS = (
    "أنشئ", "انشئ", "أضف", "اضف", "عدّل", "عدل", "غيّر", "غير", "انقل",
    "أرشف", "ارشف", "أعد تعيين", "اعد تعيين", "سجّل", "سجل", "احذف", "اعتمد",
    "وافق", "ألغ", "الغ", "ادفع", "أغلق", "اغلق", "أعد فتح", "اعد فتح",
)
# English command verbs: matched on a word boundary against the casefolded text.
EN_COMMAND_WORDS = (
    "create", "add", "edit", "change", "transfer", "archive", "rehire",
    "record", "delete", "approve", "reject", "cancel", "pay", "close", "reopen",
)
ZH_COMMAND_WORDS = (
    "创建", "添加", "修改", "更改", "调动", "归档", "重新聘用", "记录",
    "删除", "批准", "拒绝", "取消", "支付", "关闭", "重新打开",
)


def _msg(language: str, ar: str, en: str, zh: str) -> str:
    return {"ar": ar, "en": en, "zh": zh}.get(language, ar)

ALLOWED_ACTIONS = {
    "create_employee",
    "edit_employee",
    "transfer_employee",
    "archive_employee",
    "rehire_employee",
    "create_leave",
    "confirm_leave",
    "cancel_leave",
    "delete_draft_leave",
    "adjust_leave_balance",
    "create_advance",
    "approve_advance",
    "reject_advance",
    "mark_advance_paid",
    "create_staff_action",
    "approve_staff_action",
    "cancel_staff_action",
    "create_manual_attendance",
    "approve_attendance_day",
    "approve_attendance_month",
    "approve_payroll_run",
    "close_payroll_run",
    "reopen_payroll_run",
}


def is_command_request(question: str) -> bool:
    """Detect write intent in any UI language; only routes to the approval
    gateway, which always requires explicit confirmation before anything runs."""
    q = " ".join(question.strip().split())
    folded = q.casefold()
    return (
        q.startswith(WRITE_PREFIXES)
        or folded.startswith(EN_WRITE_PREFIXES)
        or any(q.startswith(word) for word in COMMAND_WORDS)
        or any(folded == word or folded.startswith(word + " ") for word in EN_COMMAND_WORDS)
        or any(q.startswith(word) for word in ZH_COMMAND_WORDS)
    )


def wants_execution(question: str) -> bool:
    q = " ".join(question.strip().split())
    return q.startswith(WRITE_PREFIXES) or q.casefold().startswith(EN_WRITE_PREFIXES)


def _loopback_only(url: str) -> bool:
    host = urlparse(url).hostname
    return host in {"127.0.0.1", "localhost", "::1"}


def _parse_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"التاريخ {field} غير صالح؛ استخدم YYYY-MM-DD.") from exc


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"القيمة {field} غير رقمية.") from exc


def _plan_context(db) -> dict[str, Any]:
    employees = db.scalars(select(Employee).order_by(Employee.code)).all()
    departments = db.scalars(select(Department).where(Department.active.is_(True)).order_by(Department.name)).all()
    leave_types = db.scalars(select(LeaveType).where(LeaveType.active.is_(True)).order_by(LeaveType.id)).all()
    advances = db.scalars(select(AdvanceRequest).order_by(AdvanceRequest.id.desc()).limit(100)).all()
    staff_actions = db.scalars(select(StaffAction).order_by(StaffAction.id.desc()).limit(100)).all()
    leave_records = db.scalars(select(LeaveRecord).order_by(LeaveRecord.id.desc()).limit(100)).all()
    attendance_months = db.scalars(select(AttendanceMonth).order_by(AttendanceMonth.id.desc()).limit(36)).all()
    attendance_records = db.scalars(select(AttendanceDailyRecord).order_by(AttendanceDailyRecord.id.desc()).limit(100)).all()
    payroll_runs = db.scalars(select(PayrollRun).order_by(PayrollRun.id.desc()).limit(36)).all()
    return {
        "employees": [{"id": e.id, "code": e.code, "name": e.full_name, "status": e.status,
                       "department_id": e.department_id, "branch_id": e.branch_id} for e in employees],
        "departments": [{"id": d.id, "name": d.name} for d in departments],
        "leave_types": [{"id": t.id, "code": t.code, "name": t.name_ar} for t in leave_types],
        "leave_records": [{"id": x.id, "employee_id": x.employee_id, "status": x.status,
                           "start_date": x.start_date.isoformat(), "end_date": x.end_date.isoformat()}
                          for x in leave_records],
        "advances": [{"id": x.id, "employee_id": x.employee_id, "amount": str(x.amount),
                      "status": x.status, "advance_type": x.advance_type,
                      "request_date": x.request_date.isoformat(), "payroll_month": x.payroll_month.isoformat()} for x in advances],
        "staff_actions": [{"id": x.id, "employee_id": x.employee_id, "kind": x.kind,
                           "category": x.category, "status": x.status, "is_financial": x.is_financial} for x in staff_actions],
        "attendance_months": [{"id": x.id, "period_start": x.period_start.isoformat(),
                               "period_end": x.period_end.isoformat(), "status": x.status} for x in attendance_months],
        "attendance_records": [{"id": x.id, "employee_id": x.employee_id, "status": x.status} for x in attendance_records],
        "payroll_runs": [{"id": x.id, "period_start": x.period_start.isoformat(),
                          "period_end": x.period_end.isoformat(), "status": x.status} for x in payroll_runs],
    }


_SUMMARY_INSTRUCTIONS = {
    "ar": "أعد أيضًا summary بالعربية قصيرة. ",
    "en": "أعد أيضًا summary قصيرة باللغة الإنجليزية. ",
    "zh": "أعد أيضًا summary قصيرة باللغة الصينية. ",
}


def _bad_json_msg(language: str) -> str:
    return _msg(
        language,
        "النموذج المحلي لم يُرجع أمرًا بصيغة JSON صحيحة.",
        "The local model did not return a valid JSON command.",
        "本地模型未返回有效的 JSON 指令。",
    )


def _json_plan(question: str, context: dict[str, Any], language: str = "ar") -> dict[str, Any]:
    status = local_model_status(language)
    if not status["available"]:
        raise RuntimeError(status["message"])
    prompt = (
        "حوّل أمر مسؤول الموارد البشرية إلى JSON فقط، بدون Markdown. "
        "لا تخترع IDs؛ استخدم فقط IDs الموجودة في البيانات. "
        "action يجب أن يكون واحدًا من القائمة: " + ", ".join(sorted(ALLOWED_ACTIONS)) + ". "
        "استخدم null للقيمة غير المطلوبة. الحقول المسموحة: action, employee_id, leave_type_id, "
        "record_id, balance_days, transaction_type, advance_id, amount, advance_type, "
        "installment_count, staff_action_id, kind, category, title, is_financial, payroll_month, "
        "attendance_record_id, clock_in, clock_out, work_date, month_id, payroll_run_id, reason, "
        "start_date, end_date, charged_days, full_name, national_id, phone, address, position, "
        "department_id, branch_id, hire_date, basic_salary, daily_hours, fingerprint_id, effective_date, request_date, payroll_month, coverage_confirmed. "
        + _SUMMARY_INSTRUCTIONS.get(language, _SUMMARY_INSTRUCTIONS["ar"])
        + f"الأمر: {question}\nالبيانات المحلية: {json.dumps(context, ensure_ascii=False)}"
    )
    from app.ai_assistant import _post_json
    try:
        response = _post_json(
            "/api/chat",
            {
                "model": status["model"],
                "stream": False,
                "format": "json",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "أنت محلل أوامر لنظام HR محلي. لا تنفذ أي شيء بنفسك. "
                            "أخرج JSON صالحًا فقط. لا تقترح SQL أو shell أو HTTP أو ملفات."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=30,
        )
    except OSError as exc:
        raise RuntimeError(_msg(
            language,
            "لم يستجب النموذج المحلي في الوقت المناسب.",
            "The local model did not respond in time.",
            "本地模型未及时响应。",
        )) from exc
    except ValueError as exc:  # transport body was not valid JSON/UTF-8
        raise ValueError(_bad_json_msg(language)) from exc
    message = response.get("message") if isinstance(response, dict) else None
    raw = str(message.get("content", "") if isinstance(message, dict) else "").strip()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(_bad_json_msg(language)) from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("action"), str) or plan.get("action") not in ALLOWED_ACTIONS:
        raise ValueError(_msg(
            language,
            "الأمر المطلوب غير مسموح به في المساعد.",
            "The requested command is not allowed in the assistant.",
            "助手不允许该请求指令。",
        ))
    validate_plan_values(plan, language)
    return plan


def build_plan(question: str, language: str = "ar") -> dict[str, Any]:
    with SessionLocal() as db:
        return _json_plan(question, _plan_context(db), language=language)


@dataclass(frozen=True)
class ExecutionResult:
    """Structured execution outcome: renderer key + display params.

    The Arabic rendering of these params is the audit original, stored
    verbatim in the audit log and conversation memory; other languages are
    presentation-only renderings by app.ai_execution_render."""

    key: str
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def audit(self) -> str:
        from app.ai_execution_render import render_execution_result

        return render_execution_result(self, "ar")

    def __str__(self) -> str:
        return self.audit


def execute_plan(plan: dict[str, Any], actor: str, *, db=None, human_coverage_confirmed=False) -> ExecutionResult:
    validate_plan_values(plan)
    if type(human_coverage_confirmed) is not bool:
        raise ValueError("Human confirmation must be explicit boolean.")
    action = plan.get("action")
    reason = str(plan.get("reason") or "").strip() or "تنفيذ بأمر صريح من مسؤول الموارد البشرية عبر المساعد المحلي."
    if action not in ALLOWED_ACTIONS:
        raise ValueError("الأمر غير مسموح.")

    with (SessionLocal.begin() if db is None else nullcontext(db)) as db:  # noqa: PLR1704
        if action == "create_manual_attendance":
            work_date = _parse_date(plan["work_date"], "تاريخ العمل")
            try:
                clock_in = datetime.fromisoformat(str(plan["clock_in"]))
                clock_out = datetime.fromisoformat(str(plan["clock_out"]))
            except (TypeError, ValueError) as exc:
                raise ValueError("يجب إدخال وقت الدخول والخروج بصيغة تاريخ ووقت صحيحة.") from exc
            record = add_manual_attendance(
                db,
                employee_id=int(plan["employee_id"]),
                work_date=work_date,
                clock_in=clock_in,
                clock_out=clock_out,
                reason=reason,
                actor=actor,
            )
            return ExecutionResult(action, {"record_id": record.id})

        if action == "create_employee":
            full_name = str(plan.get("full_name") or "").strip()
            position = str(plan.get("position") or "").strip()
            department_id = int(plan["department_id"])
            branch_id = int(plan["branch_id"]) if plan.get("branch_id") is not None else None
            hire_date = _parse_date(plan.get("hire_date") or date.today().isoformat(), "التعيين")
            basic_salary = _decimal(plan.get("basic_salary"), "الراتب")
            daily_hours = _decimal(plan.get("daily_hours") or 8, "ساعات العمل")
            if not full_name or not position or basic_salary < 0 or not (0 < daily_hours <= 24):
                raise ValueError("بيانات الموظف الأساسية غير صحيحة.")
            department = db.get(Department, department_id)
            if department is None or not department.active:
                raise ValueError("القسم غير متاح.")
            branch = db.get(Branch, branch_id) if branch_id is not None else None
            if branch_id is not None and (branch is None or not branch.active or branch.department_id != department_id):
                raise ValueError("الفرع غير تابع للقسم.")
            national_id = str(plan.get("national_id") or "").strip() or None
            if national_id and not is_valid_egyptian_national_id(national_id):
                raise ValueError(NATIONAL_ID_ERROR)
            fingerprint_id = str(plan.get("fingerprint_id") or "").strip() or None
            for field, value in (("national_id", national_id), ("fingerprint_id", fingerprint_id)):
                if value and db.scalar(select(Employee.id).where(getattr(Employee, field) == value).limit(1)):
                    raise ValueError(f"{field} مستخدم بالفعل.")
            employee = Employee(code=allocate_employee_code(db), full_name=full_name,
                national_id=national_id, phone=str(plan.get("phone") or "").strip() or None,
                address=str(plan.get("address") or "").strip() or None,
                department_id=department_id, branch_id=branch_id, position=position,
                hire_date=hire_date, basic_salary=basic_salary, daily_hours=daily_hours,
                fingerprint_id=fingerprint_id, status="active")
            db.add(employee); db.flush()
            db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=hire_date, start_reason=reason))
            db.add(AuditLog(actor=actor, action="employee_created_by_ai", entity_type="employee",
                entity_id=str(employee.id), new_value=f"code={employee.code}; name={employee.full_name}", reason=reason))
            return ExecutionResult(action, {"name": employee.full_name, "employee_id": employee.id, "code": employee.code})

        if action == "edit_employee":
            employee = db.get(Employee, int(plan["employee_id"]))
            if employee is None: raise ValueError("الموظف غير موجود.")
            fields = ("full_name", "position", "department_id", "branch_id", "basic_salary", "daily_hours",
                      "phone", "address", "national_id", "fingerprint_id")
            if "full_name" in plan and not str(plan["full_name"]).strip():
                raise ValueError("اسم الموظف لا يمكن أن يكون فارغًا.")
            if "position" in plan and not str(plan["position"]).strip():
                raise ValueError("المسمى الوظيفي لا يمكن أن يكون فارغًا.")
            if "department_id" in plan or "branch_id" in plan:
                department_id = int(plan["department_id"]) if plan.get("department_id") is not None else employee.department_id
                branch_id = int(plan["branch_id"]) if plan.get("branch_id") is not None else employee.branch_id
                department = db.get(Department, department_id)
                if department is None or not department.active:
                    raise ValueError("القسم المحدد غير متاح.")
                if branch_id is not None:
                    branch = db.get(Branch, branch_id)
                    if branch is None or not branch.active or branch.department_id != department_id:
                        raise ValueError("الفرع المحدد غير تابع للقسم.")
            for field in fields:
                if field in plan and plan[field] is not None:
                    new_value = plan[field]
                    if field == "fingerprint_id":
                        new_value = str(new_value).strip()  # same normalisation as the form and create_employee
                    if field in {"department_id", "branch_id"}:
                        new_value = int(new_value)
                    if field in {"basic_salary", "daily_hours"}:
                        new_value = _decimal(new_value, field)
                    if field in {"basic_salary", "daily_hours"} and (
                        new_value < 0 or (field == "daily_hours" and not (0 < new_value <= 24))
                    ):
                        raise ValueError("بيانات الراتب أو ساعات العمل غير صحيحة.")
                    if field == "national_id":
                        new_value = str(new_value).strip() or None
                        if (
                            new_value is not None
                            and new_value != employee.national_id
                            and (employee.nationality or "egyptian") == "egyptian"
                            and not is_valid_egyptian_national_id(new_value)
                        ):
                            raise ValueError(NATIONAL_ID_ERROR)
                    old_value = getattr(employee, field)
                    if old_value != new_value:
                        setattr(employee, field, new_value)
                        db.add(EmployeeChange(employee_id=employee.id, field_name=field,
                            old_value=str(old_value) if old_value is not None else None,
                            new_value=str(new_value) if new_value is not None else None,
                            effective_date=_parse_date(plan.get("effective_date") or date.today().isoformat(), "السريان"),
                            reason=reason))
            db.add(AuditLog(actor=actor, action="employee_updated_by_ai", entity_type="employee",
                entity_id=str(employee.id), reason=reason))
            return ExecutionResult(action, {"name": employee.full_name})

        if action == "transfer_employee":
            employee = db.get(Employee, int(plan["employee_id"]))
            if employee is None or employee.status != "active": raise ValueError("الموظف غير موجود أو غير نشط.")
            department_id = int(plan["department_id"])
            branch_id = int(plan["branch_id"]) if plan.get("branch_id") is not None else None
            department = db.get(Department, department_id)
            branch = db.get(Branch, branch_id) if branch_id is not None else None
            if department is None or not department.active: raise ValueError("القسم الجديد غير متاح.")
            if branch_id is not None and (branch is None or not branch.active or branch.department_id != department_id):
                raise ValueError("الفرع الجديد غير صالح.")
            effective = _parse_date(plan.get("effective_date") or date.today().isoformat(), "السريان")
            old_department, old_branch = employee.department_id, employee.branch_id
            employee.department_id, employee.branch_id = department_id, branch_id
            for field, old_value, new_value in (("department_id", old_department, department_id), ("branch_id", old_branch, branch_id)):
                if old_value != new_value:
                    db.add(EmployeeChange(employee_id=employee.id, field_name=field,
                        old_value=str(old_value) if old_value is not None else None,
                        new_value=str(new_value) if new_value is not None else None,
                        effective_date=effective, reason=reason))
            db.add(AuditLog(actor=actor, action="employee_transferred_by_ai", entity_type="employee",
                entity_id=str(employee.id), reason=reason))
            return ExecutionResult(action, {"name": employee.full_name})

        if action == "archive_employee":
            employee = db.get(Employee, int(plan["employee_id"]))
            if employee is None or employee.status != "active": raise ValueError("الموظف غير موجود أو غير نشط.")
            period = db.scalar(select(EmployeeServicePeriod).where(
                EmployeeServicePeriod.employee_id == employee.id, EmployeeServicePeriod.end_date.is_(None)))
            if period is None: raise ValueError("لا توجد فترة خدمة مفتوحة.")
            period.end_date = date.today(); period.end_reason = reason; employee.status = "archived"
            db.add(AuditLog(actor=actor, action="employee_archived_by_ai", entity_type="employee",
                entity_id=str(employee.id), old_value="active", new_value="archived", reason=reason))
            return ExecutionResult(action, {"name": employee.full_name})

        if action == "rehire_employee":
            employee = db.get(Employee, int(plan["employee_id"]))
            if employee is None or employee.status != "archived": raise ValueError("الموظف غير مؤرشف.")
            new_hire = _parse_date(plan.get("hire_date") or date.today().isoformat(), "إعادة التعيين")
            previous = db.scalar(select(EmployeeServicePeriod).where(
                EmployeeServicePeriod.employee_id == employee.id).order_by(EmployeeServicePeriod.id.desc()).limit(1))
            if previous is None or previous.end_date is None or new_hire <= previous.end_date:
                raise ValueError("تاريخ إعادة التعيين غير صالح.")
            employee.status = "active"; employee.hire_date = new_hire
            db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=new_hire, start_reason=reason))
            db.add(AuditLog(actor=actor, action="employee_rehired_by_ai", entity_type="employee",
                entity_id=str(employee.id), old_value="archived", new_value="active", reason=reason))
            return ExecutionResult(action, {"name": employee.full_name})

        if action == "create_leave":
            try:
                record = create_leave_draft(
                    db, employee_id=int(plan["employee_id"]), leave_type_id=int(plan["leave_type_id"]),
                    start_date=_parse_date(plan["start_date"], "البداية"),
                    end_date=_parse_date(plan["end_date"], "النهاية"),
                    charged_days=_decimal(plan["charged_days"], "الأيام"),
                    reason=reason, actor=actor, audit_action="ai_leave_record_created",
                )
            except LeaveDraftError as exc:
                messages = {"employee": "الموظف أو نوع الإجازة غير صالح.",
                            "overlap": "يوجد تداخل مع إجازة أخرى.",
                            "dates": "بيانات الإجازة غير صحيحة.", "days": "بيانات الإجازة غير صحيحة."}
                raise ValueError(messages[exc.code]) from exc
            employee = db.get(Employee, record.employee_id)
            return ExecutionResult(action, {"record_id": record.id, "name": employee.full_name})

        if action == "confirm_leave":
            record = db.scalar(select(LeaveRecord).where(LeaveRecord.id == int(plan["record_id"])).with_for_update())
            if record is None: raise ValueError("سجل الإجازة غير موجود.")
            confirm_leave(db, record, actor)
            return ExecutionResult(action, {"record_id": record.id})

        if action == "cancel_leave":
            record = db.scalar(select(LeaveRecord).where(LeaveRecord.id == int(plan["record_id"])).with_for_update())
            if record is None: raise ValueError("سجل الإجازة غير موجود.")
            cancel_leave(db, record, actor, reason)
            return ExecutionResult(action, {"record_id": record.id})

        if action == "delete_draft_leave":
            record = db.scalar(select(LeaveRecord).where(LeaveRecord.id == int(plan["record_id"])).with_for_update())
            if record is None:
                raise ValueError("سجل الإجازة غير موجود.")
            if record.status != "draft":
                raise ValueError("الحذف مسموح لمسودة الإجازة فقط؛ استخدم الإلغاء للسجلات الأخرى.")
            linked = db.scalar(select(LeaveBalanceTransaction.id).where(
                LeaveBalanceTransaction.leave_record_id == record.id
            ).limit(1))
            if linked is not None:
                raise ValueError("لا يمكن حذف إجازة لها حركة رصيد مرتبطة.")
            old_value = (
                f"employee_id={record.employee_id}; dates={record.start_date}|{record.end_date}; "
                f"charged_days={record.charged_days}; status={record.status}"
            )
            record_id = record.id
            db.delete(record)
            db.add(AuditLog(
                actor=actor, action="ai_draft_leave_deleted", entity_type="leave_record",
                entity_id=str(record_id), old_value=old_value, new_value="deleted",
                reason=reason,
            ))
            return ExecutionResult(action, {"record_id": record_id})

        if action == "adjust_leave_balance":
            employee = db.get(Employee, int(plan["employee_id"]))
            leave_type = db.get(LeaveType, int(plan["leave_type_id"]))
            days = _decimal(plan["balance_days"], "الرصيد")
            validate_manual_balance_change(str(plan.get("transaction_type") or "adjustment"), days)
            if employee is None or leave_type is None: raise ValueError("الموظف أو نوع الإجازة غير موجود.")
            expected_snapshot = plan.get("_confirmation_snapshot")
            expected_balance = (
                expected_snapshot.get("current", {}).get("current_balance")
                if isinstance(expected_snapshot, dict) else None
            )
            if expected_balance is not None:
                actual_balance = str(leave_balance(db, employee.id, leave_type.id))
                if actual_balance != str(expected_balance):
                    raise ValueError("تغير رصيد الإجازة بعد عرض المعاينة. أعد تجهيز الأمر وراجع القيم الجديدة.")
            add_balance_entry(db, employee_id=employee.id, leave_type=leave_type,
                              transaction_type=str(plan.get("transaction_type") or "adjustment"),
                              days_delta=days, reason=reason, actor=actor)
            return ExecutionResult(action, {"leave_type": leave_type.name_ar, "name": employee.full_name, "days": days})

        if action in {"create_advance", "approve_advance", "reject_advance", "mark_advance_paid"}:
            req = db.get(AdvanceRequest, int(plan.get("advance_id") or 0))
            if action == "create_advance":
                request_date = _parse_date(plan.get("request_date") or date.today().isoformat(), "الطلب")
                req = create_advance(db, employee_id=int(plan["employee_id"]), request_date=request_date,
                                     amount=_decimal(plan["amount"], "المبلغ"), reason=reason,
                                     advance_type=str(plan.get("advance_type") or "regular"), actor=actor)
                return ExecutionResult(action, {"request_id": req.id})
            if req is None: raise ValueError("طلب السلفة غير موجود.")
            if action == "approve_advance":
                approve_advance(db, req, actor, int(plan.get("installment_count") or 1))
                return ExecutionResult(action, {"request_id": req.id})
            if action == "reject_advance":
                reject_advance(db, req, actor, reason)
                return ExecutionResult(action, {"request_id": req.id})
            mark_advance_paid(db, req, actor)
            return ExecutionResult(action, {"request_id": req.id})

        if action in {"create_staff_action", "approve_staff_action", "cancel_staff_action"}:
            if action == "create_staff_action":
                from app.staff_actions_workflow import create_staff_action
                item = create_staff_action(
                    db, employee_id=int(plan["employee_id"]), kind=str(plan["kind"]),
                    category=str(plan["category"]), title=str(plan["title"]),
                    is_financial=bool(plan.get("is_financial")),
                    amount=_decimal(plan["amount"], "المبلغ") if plan.get("amount") is not None else None,
                    payroll_month=_parse_date(plan["payroll_month"], "شهر الرواتب") if plan.get("payroll_month") else None,
                    reason=reason, actor=actor)
                return ExecutionResult(action, {"item_id": item.id})
            item = db.get(StaffAction, int(plan.get("staff_action_id") or 0))
            if item is None: raise ValueError("إجراء الموظف غير موجود.")
            if action == "approve_staff_action": approve_staff_action(db, item, actor); return ExecutionResult(action, {"item_id": item.id})
            cancel_staff_action(db, item, actor, reason); return ExecutionResult(action, {"item_id": item.id})

        if action == "approve_attendance_day":
            record = db.get(AttendanceDailyRecord, int(plan["attendance_record_id"]))
            if record is None: raise ValueError("سجل الحضور غير موجود.")
            approve_attendance_day(db, record_id=record.id,
                clock_in=__import__("datetime").datetime.fromisoformat(str(plan["clock_in"])),
                clock_out=__import__("datetime").datetime.fromisoformat(str(plan["clock_out"])),
                actor=actor, reason=reason)
            return ExecutionResult(action, {"record_id": record.id})

        if action == "approve_attendance_month":
            approve_attendance_month(db, month_id=int(plan["month_id"]), actor=actor,
                                     reason=reason, coverage_confirmed=human_coverage_confirmed)
            return ExecutionResult(action, {"month_id": plan["month_id"]})

        if action in {"approve_payroll_run", "close_payroll_run", "reopen_payroll_run"}:
            from app.overtime_day_award import monthly_lock_scope
            monthly_lock_scope(db)
            run = db.scalar(select(PayrollRun).where(PayrollRun.id == int(plan["payroll_run_id"])).with_for_update())
            if run is None: raise ValueError("مسير الرواتب غير موجود.")
            if action == "approve_payroll_run": approve_payroll_run(db, run, actor); return ExecutionResult(action, {"run_id": run.id})
            if action == "close_payroll_run": close_payroll_run(db, run, actor); return ExecutionResult(action, {"run_id": run.id})
            reopen_payroll_run(db, run, actor, reason); return ExecutionResult(action, {"run_id": run.id})

    raise ValueError("لم يتم تنفيذ الأمر.")


def run_ai_command(question: str, actor: str) -> dict[str, Any]:
    """Prepare an approval-bound operation; never execute from this API."""
    if not _loopback_only(__import__("app.ai_assistant", fromlist=["OLLAMA_BASE_URL"]).OLLAMA_BASE_URL):
        raise RuntimeError("تم منع الاتصال لأن عنوان نموذج الذكاء المحلي ليس على الجهاز نفسه.")

    # A command request, including an explicit execute prefix, is not consent.
    # All writes must use the persisted, actor-bound, single-use approval gateway.
    from app.ai_gateway import prepare_command

    pending = prepare_command(question, actor)
    return {
        "executed": False,
        "pending": True,
        "token": pending["token"],
        "plan": pending["plan"],
        "summary": pending["summary"],
        "expires_at": pending["expires_at"],
        "message": "تم تجهيز العملية فقط. يلزم تأكيد مستقل وصريح عبر بوابة الموافقات قبل التنفيذ.",
    }
