"""Arabic display labels for audit log action and entity codes.




The stored codes stay unchanged; this map is presentation-only, exactly like
the report enum display maps. Unknown codes fall back to the raw value.
"""

STATUS_LABELS_AR = {
    "active": "نشط",
    "archived": "مؤرشف",
    "approved": "معتمد",
    "applied": "مطبق",
    "cancelled": "ملغى",
    "canceled": "ملغى",
    "closed": "مغلق",
    "confirmed": "مؤكد",
    "declined": "مرفوض",
    "deducted": "مخصوم",
    "draft": "مسودة",
    "failed": "فاشلة",
    "imported": "مستوردة",
    "needs_review": "تحتاج مراجعة",
    "open": "مفتوح",
    "paid": "مدفوع",
    "parsed": "مقروءة",
    "partially_paid": "مدفوع جزئيًا",
    "pending": "معلق",
    "rejected": "مرفوض",
    "reopened": "معاد فتحه",
    "scheduled": "مجدول",
    "success": "ناجحة",
    "unpaid": "غير مدفوع",
    "opening": "رصيد افتتاحي",
    "adjustment": "تسوية",
    "leave_use": "استهلاك إجازة",
    "leave_reversal": "استرجاع إجازة",
}

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
    "payroll_payment_corrected": "تصحيح دفعة راتب",
    "payroll_payment_recorded": "تسجيل دفعة راتب",
    "absence_leave_conflict_reviewed": "مراجعة تعارض غياب مع إجازة مدفوعة",
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
    # Bare status transitions (old_value/new_value like "draft") localize too.
    status_ar = STATUS_LABELS_AR.get(value.strip())
    if status_ar is not None:
        return _translate_text(status_ar, language)
    # Leave-record payloads "YYYY-MM-DD|YYYY-MM-DD|days" render as a readable
    # event line; unknown pipe payloads fall through untouched.
    pipe = _re.fullmatch(r"(?P<d1>\d{4}-\d{2}-\d{2})\|(?P<d2>\d{4}-\d{2}-\d{2})\|(?P<days>[\d.]+)", value.strip())
    if pipe is not None:
        fmt = lambda iso: f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}"
        template = _translate_text("من {d1} إلى {d2} · عدد الأيام: {days}", language)
        return template.format(d1=fmt(pipe.group("d1")), d2=fmt(pipe.group("d2")), days=pipe.group("days"))

    def _replace(match):
        prefix, key = match.group(1), match.group(2)
        label_ar = _FIELD_LABELS_AR.get(key)
        if label_ar is None:
            return match.group(0)
        return f"{prefix}{_translate_text(label_ar, language)}="

    labeled = _KEY_AT_SEGMENT_START.sub(_replace, value)
    # Missing values stored as Python None render as a dash, not "None".
    return _re.sub(r"=None(?=\s*(;|$))", "=\u2014", labeled)


# --- App-generated audit reason localization -------------------------------
# Maps (action, exact reason) pairs the application itself writes to their
# Arabic source text. Reasons outside this map (user-typed notes, historical
# payloads, AI-supplied reasons) render verbatim: the (action, reason) pair is
# the discriminator, so a coincidental user phrase is never "translated".

REASON_LABELS_AR = {
    ("advance_paid", "Payment confirmed by HR administrator."): "تم تأكيد الصرف بواسطة مسؤول الموارد البشرية.",
    ("advance_requested", "Idempotency key for the advance request form."): "مفتاح منع التكرار لنموذج طلب السلفة.",
    ("advance_submission_accepted", "Idempotency key for the advance request form."): "مفتاح منع التكرار لنموذج طلب السلفة.",
    ("attendance_day_proposed", "Attendance reconciliation proposal saved for HR review; not approved"): "حفظ اقتراح مطابقة الحضور لمراجعة الموارد البشرية؛ غير معتمد",
    ("attendance_import_batch_replaced", "HR replaced an unresolved source import before monthly reconciliation"): "استبدل مسؤول الموارد البشرية استيرادًا غير محسوم قبل المطابقة الشهرية",
    ("attendance_import_created", "Imported attendance evidence; pending reconciliation and HR review"): "دليل حضور مستورد؛ بانتظار المطابقة ومراجعة الموارد البشرية",
    ("attendance_import_unknown_employee_resolved", "HR explicitly matched an unknown source identity; raw source values retained"): "طابق مسؤول الموارد البشرية هوية مصدرية غير معروفة يدويًا؛ القيم المصدرية الخام محفوظة",
    ("attendance_month_reconciliation_created", "Monthly attendance reconciliation created; pending HR review and approval"): "أُنشئت مطابقة حضور شهرية؛ بانتظار مراجعة واعتماد الموارد البشرية",
    ("branch_created", "Branch created by HR"): "أُنشئ الفرع بواسطة الموارد البشرية",
    ("branch_updated", "Branch settings updated"): "عُدلت إعدادات الفرع",
    ("department_created", "Department created by HR"): "أُنشئ القسم بواسطة الموارد البشرية",
    ("department_updated", "Department settings updated"): "عُدلت إعدادات القسم",
    ("employee_created", "Initial employee record created"): "أُنشئ سجل الموظف المبدئي",
    ("leave_type_created", "Created by HR administrator"): "أُنشئ بواسطة مسؤول الموارد البشرية",
    ("overtime_approved", "HR administrator approved overtime."): "اعتمد مسؤول الموارد البشرية الساعات الإضافية.",
    ("payroll_item_updated", "HR administrator edited payroll values."): "عدّل مسؤول الموارد البشرية قيم الرواتب.",
    ("absence_leave_conflict_reviewed", "HR reviewed absence deduction against confirmed paid leave."): "راجع مسؤول الموارد البشرية خصم الغياب مقابل إجازة مدفوعة مؤكدة.",
    ("payroll_run_approved", "HR administrator approved payroll after review."): "اعتمد مسؤول الموارد البشرية الرواتب بعد المراجعة.",
    ("payroll_run_closed", "Payroll period closed after approval."): "أُغلقت فترة الرواتب بعد الاعتماد.",
    ("payroll_run_created", "Created after approved attendance for the exact month."): "أُنشئ بعد حضور معتمد للشهر نفسه.",
    ("security_roster_day_approved", "Administrator confirmed reviewed security roster day"): "أكد المسؤول مراجعة يوم جدول الأمن",
    ("ai_command_approved", "موافقة صريحة من مسؤول الموارد البشرية."): "موافقة صريحة من مسؤول الموارد البشرية.",
    ("ai_command_expired", "انتهت صلاحية طلب الموافقة."): "انتهت صلاحية طلب الموافقة.",
    ("ai_command_invalidated", "تغيرت بيانات السجل بعد عرض المعاينة وقبل التأكيد."): "تغيرت بيانات السجل بعد عرض المعاينة وقبل التأكيد.",
    ("ai_command_invalidated", "خطة التنفيذ المخزنة غير صالحة أو لم تعد مطابقة للخطة المعروضة."): "خطة التنفيذ المخزنة غير صالحة أو لم تعد مطابقة للخطة المعروضة.",
    ("employee_action_acknowledgement_recorded", "HR recorded that the employee viewed the notice."): "سجلت الموارد البشرية أن الموظف اطلع على الإخطار.",
}


# Pattern reasons carry a runtime value, so exact match cannot cover them.
_REASON_PATTERNS = [
    ("login_lockout", _re.compile(r"^(?P<n>\d+) failed login attempts$"), "{n} محاولة دخول فاشلة"),
    ("backup_restore_rolled_back",
     _re.compile(r"^Restore failed and previous database was restored: (?P<suffix>.*)$"),
     "فشلت الاستعادة وأُعيدت قاعدة البيانات السابقة: {suffix}"),
    ("leave_balance_leave_use",
     _re.compile(r"^Confirmed leave record #(?P<n>\d+)$"),
     "تأكيد سجل الإجازة رقم {n}"),
    ("leave_balance_leave_reversal",
     _re.compile(r"^Cancellation of leave record #(?P<n>\d+): (?P<suffix>.*)$"),
     "إلغاء سجل الإجازة رقم {n}: {suffix}"),
]


def _pattern_reason_ar(action: str, reason: str):
    for expected_action, pattern, template_ar in _REASON_PATTERNS:
        if action != expected_action:
            continue
        match = pattern.match(reason)
        if match:
            return template_ar, match.groupdict()
    return None


def reason_label(action: str, reason: str, language: str) -> str:
    """Localize known app-generated reasons; preserve all other payloads."""
    if not reason:
        return reason
    label_ar = REASON_LABELS_AR.get((action, reason))
    if label_ar is not None:
        return _translate_text(label_ar, language)
    patterned = _pattern_reason_ar(action, reason)
    if patterned is None:
        return reason
    template_ar, values = patterned
    return _translate_text(template_ar, language).format(**values)


# --- System-generated workflow reasons (balance history, employee changes) ---
# Constant prefixes localize; record numbers and user/technical suffixes stay.
_SYSTEM_REASON_EXACT = {
    "Initial employment": "التعيين المبدئي",
}

_SYSTEM_REASON_PATTERNS = [
    (_re.compile(r"^Confirmed leave record #(?P<n>\d+)$"),
     "تأكيد سجل الإجازة رقم {n}"),
    (_re.compile(r"^Cancellation of leave record #(?P<n>\d+): (?P<suffix>.*)$"),
     "إلغاء سجل الإجازة رقم {n}: {suffix}"),
    (_re.compile(r"^Approved request #(?P<n>\d+): (?P<suffix>.*)$"),
     "اعتماد الطلب رقم {n}: {suffix}"),
]


def system_reason(value: str, language: str) -> str:
    """Localize known system-generated workflow reasons; preserve all else."""
    if not value:
        return value
    label_ar = _SYSTEM_REASON_EXACT.get(value)
    if label_ar is not None:
        return _translate_text(label_ar, language)
    for pattern, template_ar in _SYSTEM_REASON_PATTERNS:
        match = pattern.match(value)
        if match:
            return _translate_text(template_ar, language).format(**match.groupdict())
    return value
