"""Reject coercion at the local model proposal boundary, not authorization."""
from decimal import Decimal, InvalidOperation
from app.ai_errors import LocalizedValueError


def validate_plan_values(plan, language='ar'):
    messages = {
        'ar': 'بيانات الأمر غير صالحة. يلزم إدخال قيم صحيحة وصريحة قبل المراجعة.',
        'en': 'Invalid command values. Supply valid explicit values before review.',
        'zh': '指令数据无效。请在审核前提供有效且明确的值。',
    }
    message = messages.get(language, messages['ar'])
    if not isinstance(plan, dict):
        raise LocalizedValueError(message, language)  # noqa: TRY004
    if 'action' in plan and not isinstance(plan['action'], str):
        raise LocalizedValueError(message, language)
    for key, value in plan.items():
        if key in {'is_financial', 'coverage_confirmed'} and value is not None and type(value) is not bool:
            raise LocalizedValueError(message, language)
        if key == 'fingerprint_id':
            # Free-text device identifier (employee form stores a stripped string); never an integer id.
            if value is not None and (type(value) is not str or not value.strip() or len(value.strip()) > 50):
                raise LocalizedValueError(message, language)
            continue
        if key.endswith('_id') and value is not None and (type(value) is not int or value < 1):
            raise LocalizedValueError(message, language)
        if key in {'amount', 'basic_salary', 'daily_hours', 'balance_days', 'charged_days'} and value is not None:
            try:
                amount = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise LocalizedValueError(message, language) from exc
            if isinstance(value, bool) or not amount.is_finite():
                raise LocalizedValueError(message, language)
    required = {
        'approve_payroll_run': ('payroll_run_id',),
        'close_payroll_run': ('payroll_run_id',),
        'reopen_payroll_run': ('payroll_run_id', 'reason'),
        'approve_attendance_month': ('month_id',),
        'approve_attendance_day': ('attendance_record_id', 'clock_in', 'clock_out'),
        'create_manual_attendance': ('employee_id', 'work_date', 'clock_in', 'clock_out', 'reason'),
        'create_staff_action': ('employee_id', 'kind', 'category', 'title', 'is_financial'),
        # Keys execute_plan reads with plan[...] (a missing key would be a KeyError at approval time).
        'create_employee': ('department_id',),
        'edit_employee': ('employee_id',),
        'transfer_employee': ('employee_id', 'department_id'),
        'archive_employee': ('employee_id',),
        'rehire_employee': ('employee_id',),
        'create_leave': ('employee_id', 'leave_type_id', 'start_date', 'end_date', 'charged_days'),
        'confirm_leave': ('record_id',),
        'cancel_leave': ('record_id',),
        'delete_draft_leave': ('record_id',),
        'adjust_leave_balance': ('employee_id', 'leave_type_id', 'balance_days'),
        'create_advance': ('employee_id', 'amount'),
    }
    for key in required.get(plan.get('action'), ()):
        value = plan.get(key)
        if value is None or value == '' or (isinstance(value, str) and not value.strip()):
            raise LocalizedValueError(message, language)
