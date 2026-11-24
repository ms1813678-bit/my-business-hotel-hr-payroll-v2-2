"""Reject coercion at the local model proposal boundary, not authorization."""
from decimal import Decimal, InvalidOperation


def validate_plan_values(plan, language='ar'):
    messages = {
        'ar': 'بيانات الأمر غير صالحة. يلزم إدخال قيم صحيحة وصريحة قبل المراجعة.',
        'en': 'Invalid command values. Supply valid explicit values before review.',
        'zh': '指令数据无效。请在审核前提供有效且明确的值。',
    }
    message = messages.get(language, messages['ar'])
    if not isinstance(plan, dict):
        raise ValueError(message)  # noqa: TRY004
    for key, value in plan.items():
        if key in {'is_financial', 'coverage_confirmed'} and value is not None and type(value) is not bool:
            raise ValueError(message)
        if key.endswith('_id') and value is not None and (type(value) is not int or value < 1):
            raise ValueError(message)
        if key in {'amount', 'basic_salary', 'daily_hours', 'balance_days', 'charged_days'} and value is not None:
            try:
                amount = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(message) from exc
            if isinstance(value, bool) or not amount.is_finite():
                raise ValueError(message)
    required = {
        'approve_payroll_run': ('payroll_run_id',),
        'close_payroll_run': ('payroll_run_id',),
        'reopen_payroll_run': ('payroll_run_id', 'reason'),
        'approve_attendance_month': ('month_id',),
        'approve_attendance_day': ('attendance_record_id', 'clock_in', 'clock_out'),
        'create_manual_attendance': ('employee_id', 'work_date', 'clock_in', 'clock_out', 'reason'),
        'create_staff_action': ('employee_id', 'kind', 'category', 'title', 'is_financial'),
    }
    for key in required.get(plan.get('action'), ()):
        if plan.get(key) is None or plan.get(key) == '':
            raise ValueError(message)
