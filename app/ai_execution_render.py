"""Trilingual rendering for AI execution results.

Execution outcomes are produced as structured results (key + params) by
``app.ai_agent.execute_plan``. The Arabic template here IS the audit original:
it is written verbatim to the audit log and to conversation memory. The
English and Chinese templates are presentation-only renderings of the same
facts; no stored text, audit record or business value is ever translated or
rewritten.
"""

from __future__ import annotations

from typing import Any

# key -> (ar audit original, en presentation, zh presentation)
_TEMPLATES: dict[str, tuple[str, str, str]] = {
    "create_manual_attendance": (
        "تم تسجيل حضور يدوي للموظف في سجل رقم {record_id}، والحالة تنتظر المراجعة والاعتماد.",
        "Manual attendance was recorded for the employee as record #{record_id}; it is pending review and approval.",
        "已为该员工登记手工考勤，记录编号 {record_id}，当前状态待审核和批准。",
    ),
    "create_employee": (
        "تم إنشاء الموظف {name} برقم {employee_id} وكود {code}.",
        "Employee {name} was created with id {employee_id} and code {code}.",
        "已创建员工 {name}，编号 {employee_id}，工号 {code}。",
    ),
    "edit_employee": (
        "تم تعديل بيانات الموظف {name}.",
        "Employee {name}'s data was updated.",
        "已更新员工 {name} 的资料。",
    ),
    "transfer_employee": (
        "تم نقل الموظف {name}.",
        "Employee {name} was transferred.",
        "已完成员工 {name} 的调动。",
    ),
    "archive_employee": (
        "تمت أرشفة الموظف {name}.",
        "Employee {name} was archived.",
        "已归档员工 {name}。",
    ),
    "rehire_employee": (
        "تمت إعادة تعيين الموظف {name}.",
        "Employee {name} was rehired.",
        "已重新录用员工 {name}。",
    ),
    "create_leave": (
        "تم إنشاء مسودة إجازة رقم {record_id} للموظف {name}.",
        "Leave draft #{record_id} was created for employee {name}.",
        "已为员工 {name} 创建休假草稿 {record_id} 号。",
    ),
    "confirm_leave": (
        "تم اعتماد الإجازة رقم {record_id}.",
        "Leave #{record_id} was approved.",
        "已批准休假 {record_id} 号。",
    ),
    "cancel_leave": (
        "تم إلغاء الإجازة رقم {record_id}.",
        "Leave #{record_id} was cancelled.",
        "已取消休假 {record_id} 号。",
    ),
    "delete_draft_leave": (
        "تم حذف مسودة الإجازة رقم {record_id} فقط، مع الاحتفاظ بسجل التدقيق.",
        "Only leave draft #{record_id} was deleted; the audit record is retained.",
        "仅删除了休假草稿 {record_id} 号，审计记录已保留。",
    ),
    "adjust_leave_balance": (
        "تم تعديل رصيد {leave_type} للموظف {name} بمقدار {days}.",
        "The {leave_type} balance of employee {name} was adjusted by {days}.",
        "已将员工 {name} 的 {leave_type} 余额调整 {days}。",
    ),
    "create_advance": (
        "تم إنشاء طلب سلفة رقم {request_id}.",
        "Advance request #{request_id} was created.",
        "已创建预支申请 {request_id} 号。",
    ),
    "approve_advance": (
        "تم اعتماد السلفة رقم {request_id}.",
        "Advance #{request_id} was approved.",
        "已批准预支 {request_id} 号。",
    ),
    "reject_advance": (
        "تم رفض السلفة رقم {request_id}.",
        "Advance #{request_id} was rejected.",
        "已拒绝预支 {request_id} 号。",
    ),
    "mark_advance_paid": (
        "تم تسجيل سداد السلفة رقم {request_id}.",
        "Advance #{request_id} was marked as paid.",
        "已将预支 {request_id} 号登记为已支付。",
    ),
    "create_staff_action": (
        "تم إنشاء إجراء موظف رقم {item_id}.",
        "Staff action #{item_id} was created.",
        "已创建员工处分/措施 {item_id} 号。",
    ),
    "approve_staff_action": (
        "تم اعتماد إجراء الموظف رقم {item_id}.",
        "Staff action #{item_id} was approved.",
        "已批准员工处分/措施 {item_id} 号。",
    ),
    "cancel_staff_action": (
        "تم إلغاء إجراء الموظف رقم {item_id}.",
        "Staff action #{item_id} was cancelled.",
        "已取消员工处分/措施 {item_id} 号。",
    ),
    "approve_attendance_day": (
        "تم اعتماد سجل الحضور رقم {record_id}.",
        "Attendance record #{record_id} was approved.",
        "已批准考勤记录 {record_id} 号。",
    ),
    "approve_attendance_month": (
        "تم اعتماد شهر الحضور رقم {month_id}.",
        "Attendance month #{month_id} was approved.",
        "已批准考勤月份 {month_id} 号。",
    ),
    "approve_payroll_run": (
        "تم اعتماد مسير الرواتب رقم {run_id}.",
        "Payroll run #{run_id} was approved.",
        "已批准工资批次 {run_id} 号。",
    ),
    "close_payroll_run": (
        "تم إغلاق مسير الرواتب رقم {run_id}.",
        "Payroll run #{run_id} was closed.",
        "已关闭工资批次 {run_id} 号。",
    ),
    "reopen_payroll_run": (
        "تم إعادة فتح مسير الرواتب رقم {run_id}.",
        "Payroll run #{run_id} was reopened.",
        "已重新打开工资批次 {run_id} 号。",
    ),
}


def render_execution_result(result: Any, language: str) -> str:
    """Render a structured execution result in the requested language.

    The Arabic rendering is the audit-original wording. Unknown keys render
    the audit text unchanged in every language (never invented).
    """
    key = getattr(result, "key", "")
    params = getattr(result, "params", {}) or {}
    templates = _TEMPLATES.get(key)
    if templates is None:
        # unknown key: never invent a rendering; pass through a carried audit
        # text verbatim, or fall back to the bare action key
        return str(params.get("audit") or key)
    ar, en, zh = templates
    template = {"ar": ar, "en": en, "zh": zh}.get(language, ar)
    return template.format(**params)
