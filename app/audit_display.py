"""Arabic display labels for audit log action and entity codes.

The stored codes stay unchanged; this map is presentation-only, exactly like
the report enum display maps. Unknown codes fall back to the raw value.
"""

ACTION_LABELS = {
    "admin_password_changed": "تغيير كلمة مرور المسؤول",
    "login_lockout": "قفل الدخول بعد محاولات غير صحيحة",
    "advance_approved": "اعتماد سلفة",
    "advance_paid": "صرف سلفة",
    "advance_rejected": "رفض سلفة",
    "advance_requested": "طلب سلفة",
    "advance_submission_accepted": "قبول تقديم سلفة",
    "ai_command_approved": "اعتماد أمر مساعد الذكاء الاصطناعي",
    "ai_command_executed": "تنفيذ أمر مساعد الذكاء الاصطناعي",
    "ai_command_expired": "انتهاء صلاحية أمر مساعد الذكاء الاصطناعي",
    "ai_command_failed": "فشل أمر مساعد الذكاء الاصطناعي",
    "ai_command_invalidated": "إبطال أمر مساعد الذكاء الاصطناعي",
    "ai_command_proposed": "اقتراح أمر مساعد الذكاء الاصطناعي",
    "ai_command_rejected": "رفض أمر مساعد الذكاء الاصطناعي",
    "ai_draft_leave_deleted": "حذف مسودة إجازة من مساعد الذكاء الاصطناعي",
    "ai_leave_record_created": "تسجيل إجازة بواسطة مساعد الذكاء الاصطناعي",
    "attendance_day_approved": "اعتماد يوم حضور",
    "attendance_day_proposed": "اقتراح يوم حضور",
    "attendance_import_batch_replaced": "استبدال دفعة استيراد حضور",
    "attendance_import_created": "إنشاء استيراد حضور",
    "attendance_import_unknown_employee_resolved": "حل موظف غير معروف في الاستيراد",
    "attendance_manual_entry_created": "إدخال حضور يدوي",
    "attendance_month_approved": "اعتماد شهر حضور",
    "attendance_month_reconciliation_created": "إنشاء مطابقة حضور شهرية",
    "backup_restore_rolled_back": "التراجع عن استعادة نسخة",
    "backup_restored": "استعادة نسخة احتياطية",
    "branch_created": "إضافة فرع",
    "branch_updated": "تعديل فرع",
    "department_created": "إضافة قسم",
    "department_updated": "تعديل قسم",
    "employee_action_acknowledgement_recorded": "تسجيل إقرار موظف بإجراء",
    "employee_archived": "أرشفة موظف",
    "employee_archived_by_ai": "أرشفة موظف بواسطة مساعد الذكاء الاصطناعي",
    "employee_created": "إضافة موظف",
    "employee_created_by_ai": "إضافة موظف بواسطة مساعد الذكاء الاصطناعي",
    "employee_data_change_requested": "طلب تصحيح بيانات موظف",
    "employee_rehired": "إعادة تعيين موظف",
    "employee_rehired_by_ai": "إعادة تعيين موظف بواسطة مساعد الذكاء الاصطناعي",
    "employee_transferred": "نقل موظف",
    "employee_transferred_by_ai": "نقل موظف بواسطة مساعد الذكاء الاصطناعي",
    "employee_updated": "تعديل بيانات موظف",
    "employee_updated_by_ai": "تعديل موظف بواسطة مساعد الذكاء الاصطناعي",
    "leave_balance_opening": "رصيد إجازات افتتاحي",
    "leave_balance_adjustment": "تسوية رصيد إجازات",
    "leave_balance_leave_use": "خصم استخدام إجازة من الرصيد",
    "leave_balance_leave_reversal": "رد خصم إجازة للرصيد",
    "leave_cancelled": "إلغاء إجازة",
    "leave_confirmed": "تأكيد إجازة",
    "leave_record_created": "تسجيل إجازة",
    "leave_type_created": "إضافة نوع إجازة",
    "overtime_approved": "اعتماد ساعات إضافية",
    "payroll_item_updated": "تعديل بند راتب",
    "payroll_payment_recorded": "تسجيل دفعة راتب",
    "payroll_run_approved": "اعتماد مسير رواتب",
    "payroll_run_closed": "إغلاق مسير رواتب",
    "payroll_run_created": "إنشاء مسير رواتب",
    "payroll_run_reopened": "إعادة فتح مسير رواتب",
    "reward_approved": "اعتماد مكافأة",
    "reward_cancelled": "إلغاء مكافأة",
    "reward_created": "تسجيل مكافأة",
    "sanction_approved": "اعتماد جزاء",
    "sanction_cancelled": "إلغاء جزاء",
    "sanction_created": "تسجيل جزاء",
    "security_roster_day_approved": "اعتماد يوم جدول أمن",
}

ENTITY_LABELS = {
    "admin_account": "حساب المسؤول",
    "advance_request": "طلب سلفة",
    "advance_submission": "تقديم سلفة",
    "ai_command": "أمر مساعد الذكاء الاصطناعي",
    "attendance_daily_record": "سجل حضور يومي",
    "attendance_import_batch": "دفعة استيراد حضور",
    "attendance_import_issue": "مشكلة استيراد حضور",
    "attendance_month": "شهر حضور",
    "backup_archive": "أرشيف نسخة احتياطية",
    "backup_run": "عملية نسخ احتياطي",
    "branch": "فرع",
    "department": "قسم",
    "employee": "موظف",
    "employee_data_change_request": "طلب تصحيح بيانات",
    "employee_leave_balance": "رصيد إجازات",
    "leave_record": "سجل إجازة",
    "leave_type": "نوع إجازة",
    "payroll_item": "بند راتب",
    "payroll_payment": "دفعة راتب",
    "payroll_run": "مسير رواتب",
    "security_roster": "جدول أمن",
    "staff_action": "إجراء موظف",
}


def action_label(code: str) -> str:
    return ACTION_LABELS.get(code, code)


def entity_label(code: str) -> str:
    return ENTITY_LABELS.get(code, code)


# --- Audit detail payload label localization -------------------------------
# Stored audit old_value/new_value payloads often look like
# "code=EMP-001; full_name=...; department_id=2". The stored payload is never
# rewritten; at render time we swap only known field keys for their localized
# labels, preserving values and any unknown payload text byte-for-byte.

import re as _re

from app.i18n import translate_text as _translate_text
from app.validation_labels import FIELD_LABELS_AR as _FIELD_LABELS_AR

_KEY_AT_SEGMENT_START = _re.compile(r"(^|;\s*)([A-Za-z_][A-Za-z0-9_]*)=")


def detail_label(value: str, language: str) -> str:
    """Localize known field keys inside an audit payload; preserve the rest."""
    if not value:
        return value

    def _replace(match):
        prefix, key = match.group(1), match.group(2)
        label_ar = _FIELD_LABELS_AR.get(key)
        if label_ar is None:
            return match.group(0)
        return f"{prefix}{_translate_text(label_ar, language)}="

    return _KEY_AT_SEGMENT_START.sub(_replace, value)
