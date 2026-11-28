"""Safe, localized display of AI approve/reject failures.

Exception text is never rendered as is. A failure is shown only when it is
  1. a LocalizedMessage written in the request language (ai_errors), or
  2. an exact, known business refusal raised by the approval path (table below, keyed by the exact message or
     machine code the domain code raises), or
  3. a known parameterised refusal matched by a strict pattern (digits / employee codes only).
Anything else gets the caller's fixed generic message, with no internals.
tests/test_ai_error_display.py scans the raise sites so a new static message cannot be added unmapped.
"""
from __future__ import annotations

import re

from app.ai_errors import LocalizedMessage, LocalizedRuntimeError, LocalizedValueError, is_authored
from app.national_id import ERROR_MESSAGE
from app.payroll_workflow import AbsenceLeaveConflictError

# key (exact message or code) -> (ar, en, zh). None means "the key itself" in that language.
_REASONS: dict[str, tuple] = {
    "This month carries deductions into a later month that already exists; it cannot be reopened.": (
        "هذا الشهر يرحّل خصومات إلى شهر لاحق موجود بالفعل؛ لا يمكن إعادة فتحه.",
        "This month carries deductions into a later month that already exists; it cannot be reopened.",
        "本月的扣款已结转到已存在的后续月份，无法重新打开。"),
    # ---- leave balances ----
    "Malformed opening receipt evidence.": ("تعذر التحقق من إثبات الرصيد الافتتاحي؛ لم يتم تسجيل التغيير.", "Opening receipt evidence could not be verified; no change was recorded.", "无法验证期初余额凭据；未记录变更。"),
    "Manual balance changes must be opening or adjustment; carry and settlement require their reviewed workflow.": (
        "التغيير اليدوي للرصيد يجب أن يكون افتتاحيًا أو تسوية؛ الترحيل والتصفية يتطلبان مسارهما المراجَع.", None,
        "手动余额变更只能是期初或调整；结转和结算须走已审核的流程。"),
    "Finite nonzero Decimal days required.": ("يلزم عدد أيام رقمي محدد وغير صفري.", None, "需要有限且非零的天数。"),
    "Finite Decimal days required.": ("يلزم عدد أيام رقمي محدد.", None, "需要有限的天数。"),
    "Finite Decimal days are required.": ("يلزم عدد أيام رقمي محدد.", None, "需要有限的天数。"),
    "Negative opening is not permitted. Review negative carry as an explicit adjustment, not a new opening or grant.": (
        "الرصيد الافتتاحي السالب غير مسموح. راجع الترحيل السالب كتسوية صريحة وليس كرصيد افتتاحي أو منحة جديدة.", None,
        "不允许负数期初余额。请将负结转作为明确的调整处理，而不是新的期初或授予。"),
    "Exact arithmetic resource bound exceeded; no amount rounded or recorded.": (
        "تجاوز الحساب الدقيق الحد المسموح؛ لم يُسجَّل أي مبلغ ولم يُقرَّب.", None, "精确计算超出上限；未记录任何金额。"),
    "Leave balance evidence must be finite.": ("بيانات رصيد الإجازة يجب أن تكون أرقامًا محددة.", None, "休假余额数据必须是有限数值。"),
    "Only a draft leave record can be confirmed.": ("لا يمكن تأكيد إلا سجل إجازة في حالة مسودة.", None, "只有草稿状态的请假记录才能确认。"),
    "The selected leave type is not active.": ("نوع الإجازة المحدد غير نشط.", None, "所选休假类型未启用。"),
    "This leave type does not use a balance.": ("نوع الإجازة هذا لا يستخدم رصيدًا.", None, "该休假类型不使用余额。"),
    "A reason is required for every balance transaction.": ("يلزم سبب لكل حركة رصيد.", None, "每笔余额变动都需要填写原因。"),
    "The balance change must be greater than zero or less than zero.": ("يجب أن يكون تغيير الرصيد أكبر من صفر أو أقل منه.", None, "余额变动必须大于零或小于零。"),
    "Explicit transaction type required.": ("يلزم تحديد نوع الحركة صراحة.", None, "需要明确的交易类型。"),
    "Unknown leave balance transaction type.": ("نوع حركة رصيد الإجازة غير معروف.", None, "未知的休假余额交易类型。"),
    "An opening balance was already recorded; use an adjustment instead.": ("سُجِّل رصيد افتتاحي بالفعل؛ استخدم تسوية بدلًا منه.", None, "已记录期初余额；请改用调整。"),
    "This leave record is already cancelled.": ("سجل الإجازة هذا ملغى بالفعل.", None, "该请假记录已取消。"),
    # ---- advances ----
    "Only a pending advance can be rejected.": ("لا يمكن رفض إلا سلفة قيد الانتظار.", None, "只有待处理的预支才能拒绝。"),
    "A rejection reason is required.": ("يلزم ذكر سبب الرفض.", None, "需要填写拒绝原因。"),
    "Only an approved advance can be marked as paid.": ("لا يمكن تسجيل الدفع إلا لسلفة معتمدة.", None, "只有已批准的预支才能标记为已支付。"),
    "Advance requests are allowed only from day 15 through day 22.": ("طلبات السلف مسموحة فقط من اليوم 15 إلى اليوم 22 من الشهر.", None, "预支申请仅限每月15日至22日。"),
    "An advance request date cannot be in the future.": ("لا يمكن أن يكون تاريخ طلب السلفة في المستقبل.", None, "预支申请日期不能晚于今天。"),
    "Advance requests must be recorded in the current month.": ("يجب تسجيل طلبات السلف في الشهر الحالي.", None, "预支申请必须在当月登记。"),
    "Invalid advance type.": ("نوع السلفة غير صالح.", None, "预支类型无效。"),
    "Enter a positive amount and a reason.": ("أدخل مبلغًا موجبًا وسببًا.", None, "请输入正数金额和原因。"),
    "The employee is not active.": ("الموظف غير نشط.", None, "该员工未在职。"),
    "The employee must have a positive basic salary.": ("يجب أن يكون للموظف راتب أساسي أكبر من صفر.", None, "员工的基本工资必须大于零。"),
    "Invalid advance submission key.": ("مفتاح تقديم السلفة غير صالح.", None, "预支提交标识无效。"),
    "The previous advance submission no longer exists.": ("طلب السلفة السابق لم يعد موجودًا.", None, "之前的预支提交已不存在。"),
    "This submission key was already used for a different advance request.": (
        "تم استخدام مفتاح التقديم هذا مسبقًا لطلب سلفة مختلف؛ لم يتم تسجيل طلب جديد.", None,
        "此提交标识已用于另一笔不同的预支申请；未记录新的申请。"),
    "The previous advance submission record is invalid.": ("سجل طلب السلفة السابق غير صالح.", None, "之前的预支提交记录无效。"),
    "The advance request no longer exists.": ("طلب السلفة لم يعد موجودًا.", None, "该预支申请已不存在。"),
    "Only a pending advance can be approved.": ("لا يمكن اعتماد إلا سلفة قيد الانتظار.", None, "只有待处理的预支才能批准。"),
    "The monthly advance limit would be exceeded.": ("سيتم تجاوز الحد الشهري للسلف.", None, "将超出每月预支上限。"),
    "The monthly advance limit would be exceeded in the deduction month.": ("سيتم تجاوز الحد الشهري للسلف في شهر الخصم الفعلي، فلا يمكن تأكيد الصرف.", None, "在实际扣款月份将超出每月预支上限，无法确认发放。"),
    "The advance has no installment schedule.": ("لا يوجد جدول أقساط لهذه السلفة.", None, "该预支没有分期计划。"),
    "The installment schedule was already used by payroll.": ("جدول أقساط هذه السلفة استُخدم بالفعل في مسير الرواتب.", None, "该预支的分期计划已被工资单使用。"),
    "The deduction start delay must be between 0 and 2 months.": ("تأخير بدء الخصم يجب أن يكون بين صفر وشهرين.", None, "扣款开始的推迟月数必须在0到2之间。"),
    "Only an installment advance can start deducting later.": ("لا يمكن تأخير بدء الخصم إلا في السلفة المقسطة.", None, "只有分期预支可以推迟开始扣款。"),
    "The payment date cannot be in the future.": ("لا يمكن أن يكون تاريخ الصرف في المستقبل.", None, "发放日期不能晚于今天。"),
    "The payment date cannot be before the request date.": ("لا يمكن أن يسبق تاريخ الصرف تاريخ الطلب.", None, "发放日期不能早于申请日期。"),
    "A reason is required when the payment month is already closed.": ("شهر تاريخ الصرف انتهت مهلته، لذلك يلزم كتابة سبب التسجيل المتأخر.", None, "该发放日期所在月份已过截止日，因此必须填写延迟登记原因。"),
    "Regular advances must be deducted as one monthly advance.": ("يجب خصم السلف العادية كسلفة شهرية واحدة.", None, "普通预支必须作为一笔月度预支扣除。"),
    "Installment count must be between 1 and 36.": ("عدد الأقساط يجب أن يكون بين 1 و36.", None, "分期数必须在1到36之间。"),
    "Advance installment schedule already exists with a different count.": ("جدول أقساط السلفة موجود بالفعل بعدد أقساط مختلف.", None, "该预支的分期计划已存在，且分期数不同。"),
    # ---- payroll ----
    "Only a closed payroll run can be reopened.": ("لا يمكن إعادة فتح إلا مسير رواتب مغلق.", None, "只有已关闭的工资单才能重新打开。"),
    "A reason is required to reopen payroll.": ("يلزم ذكر سبب لإعادة فتح مسير الرواتب.", None, "重新打开工资单需要填写原因。"),
    "Only approved payroll can be closed.": ("لا يمكن إغلاق إلا مسير رواتب معتمد.", None, "只有已批准的工资单才能关闭。"),
    "Only a draft or reopened payroll run can be approved.": ("لا يمكن اعتماد إلا مسير رواتب في حالة مسودة أو أُعيد فتحه.", None, "只有草稿或已重新打开的工资单才能批准。"),
    "Attendance approval is required before payroll approval.": ("يلزم اعتماد الحضور قبل اعتماد مسير الرواتب.", None, "批准工资单之前须先批准考勤。"),
    "A payroll run without employee items cannot be approved.": ("لا يمكن اعتماد مسير رواتب بلا بنود موظفين.", None, "没有员工明细的工资单不能批准。"),
    # ---- staff actions ----
    "This action is already cancelled.": ("هذا الإجراء ملغى بالفعل.", None, "该操作已取消。"),
    "An action reserved by or applied to payroll cannot be cancelled here.": ("لا يمكن إلغاء إجراء محجوز أو مطبّق على الرواتب من هنا.", None, "已被工资单预留或应用的操作不能在此取消。"),
    "A cancellation reason is required.": ("يلزم ذكر سبب الإلغاء.", None, "需要填写取消原因。"),
    "Only draft actions can be approved.": ("لا يمكن اعتماد إلا الإجراءات في حالة مسودة.", None, "只有草稿状态的操作才能批准。"),
    "An identical active financial action already exists.": ("يوجد بالفعل إجراء مالي نشط مطابق.", None, "已存在相同的有效财务操作。"),
    # ---- attendance (machine codes) ----
    "attendance_approving_admin_required": ("يلزم مسؤول معتمد لاعتماد الحضور.", "An authorised admin is required to approve attendance.", "批准考勤需要有权限的管理员。"),
    "attendance_month_approval_reason_required": ("يلزم ذكر سبب اعتماد شهر الحضور.", "A reason is required to approve the attendance month.", "批准考勤月份需要填写原因。"),
    "attendance_month_coverage_confirmation_required": ("يلزم تأكيد تغطية الحضور لاعتماد الشهر.", "Attendance coverage must be confirmed to approve the month.", "批准该月需要先确认考勤覆盖范围。"),
    "attendance_month_not_found": ("شهر الحضور غير موجود.", "The attendance month was not found.", "未找到该考勤月份。"),
    "attendance_month_already_approved": ("شهر الحضور معتمد بالفعل.", "The attendance month is already approved.", "该考勤月份已批准。"),
    "attendance_month_not_under_review": ("شهر الحضور ليس قيد المراجعة.", "The attendance month is not under review.", "该考勤月份不在审核中。"),
    "attendance_month_sources_incomplete": ("مصادر بيانات شهر الحضور غير مكتملة.", "The attendance month sources are incomplete.", "该考勤月份的数据来源不完整。"),
    "attendance_month_source_batch_missing": ("دفعة مصدر بيانات الحضور مفقودة.", "The attendance source batch is missing.", "缺少考勤数据来源批次。"),
    "attendance_month_source_batch_mismatch": ("دفعة مصدر الحضور لا تطابق هذا الشهر.", "The attendance source batch does not match this month.", "考勤数据来源批次与该月份不匹配。"),
    "attendance_month_source_batch_not_ready_for_approval": ("دفعة مصدر الحضور غير جاهزة للاعتماد.", "The attendance source batch is not ready for approval.", "考勤数据来源批次尚未准备好批准。"),
    "attendance_month_has_unresolved_import_issues": ("توجد مشكلات استيراد حضور غير محلولة.", "There are unresolved attendance import issues.", "存在未解决的考勤导入问题。"),
    "attendance_month_has_no_daily_records": ("لا توجد سجلات حضور يومية في هذا الشهر.", "This month has no daily attendance records.", "该月份没有每日考勤记录。"),
    "attendance_month_has_unapproved_daily_records": ("توجد سجلات حضور يومية غير معتمدة في هذا الشهر.", "This month has unapproved daily attendance records.", "该月份存在未批准的每日考勤记录。"),
    "employee_department_history_is_inconsistent": ("سجل أقسام الموظف غير متسق.", "The employee's department history is inconsistent.", "员工的部门历史记录不一致。"),
    "employee_department_history_is_incomplete": ("سجل أقسام الموظف غير مكتمل.", "The employee's department history is incomplete.", "员工的部门历史记录不完整。"),
    "attendance_clock_values_must_be_datetimes": ("يجب أن تكون أوقات الدخول والخروج تاريخًا ووقتًا صحيحين.", "Clock in and out must be valid date-times.", "上班和下班时间必须是有效的日期时间。"),
    "attendance_clock_out_must_follow_clock_in": ("يجب أن يكون وقت الخروج بعد وقت الدخول.", "Clock out must be after clock in.", "下班时间必须晚于上班时间。"),
    "attendance_approval_reason_required": ("يلزم ذكر سبب اعتماد الحضور.", "A reason is required to approve attendance.", "批准考勤需要填写原因。"),
    "attendance_daily_record_not_found": ("سجل الحضور اليومي غير موجود.", "The daily attendance record was not found.", "未找到该每日考勤记录。"),
    "attendance_daily_record_already_approved": ("سجل الحضور اليومي معتمد بالفعل.", "The daily attendance record is already approved.", "该每日考勤记录已批准。"),
    "attendance_daily_record_not_reviewable": ("سجل الحضور اليومي غير قابل للمراجعة.", "The daily attendance record cannot be reviewed.", "该每日考勤记录无法审核。"),
    "attendance_manual_actor_required": ("يلزم تحديد المسؤول الذي يسجل الحضور اليدوي.", "The admin recording the manual attendance is required.", "手动考勤需要指明记录人。"),
    "attendance_manual_reason_required": ("يلزم ذكر سبب الحضور اليدوي.", "A reason is required for manual attendance.", "手动考勤需要填写原因。"),
    "attendance_manual_work_date_required": ("يلزم تحديد تاريخ العمل للحضور اليدوي.", "A work date is required for manual attendance.", "手动考勤需要工作日期。"),
    "attendance_manual_clock_values_must_be_datetimes": ("يجب أن تكون أوقات الدخول والخروج تاريخًا ووقتًا صحيحين.", "Clock in and out must be valid date-times.", "上班和下班时间必须是有效的日期时间。"),
    "attendance_manual_clock_in_must_match_work_date": ("يجب أن يطابق وقت الدخول تاريخ العمل.", "Clock in must match the work date.", "上班时间必须与工作日期一致。"),
    "attendance_manual_clock_out_must_follow_clock_in": ("يجب أن يكون وقت الخروج بعد وقت الدخول.", "Clock out must be after clock in.", "下班时间必须晚于上班时间。"),
    "employee_not_found": ("الموظف غير موجود.", "The employee was not found.", "未找到该员工。"),
    "attendance_employee_not_in_service_on_date": ("الموظف لم يكن على رأس العمل في هذا التاريخ.", "The employee was not in service on that date.", "该员工在该日期不在职。"),
    "attendance_month_not_found_for_manual_entry": ("لا يوجد شهر حضور لهذا الإدخال اليدوي.", "No attendance month exists for this manual entry.", "该手动记录没有对应的考勤月份。"),
    "attendance_month_not_ready_for_manual_entry": ("شهر الحضور غير جاهز للإدخال اليدوي.", "The attendance month is not ready for manual entry.", "该考勤月份尚不能手动录入。"),
    # ---- ai_agent execute_plan (Arabic authored) ----
    "لم يتم تنفيذ الأمر.": (None, "The command was not carried out.", "该指令未执行。"),
    "الأمر غير مسموح.": (None, "The command is not allowed.", "该指令不被允许。"),
    "بيانات الموظف الأساسية غير صحيحة.": (None, "The employee's basic details are invalid.", "员工基本信息无效。"),
    "القسم غير متاح.": (None, "The department is not available.", "该部门不可用。"),
    "الفرع غير تابع للقسم.": (None, "The branch does not belong to the department.", "该分支不属于该部门。"),
    "الموظف غير موجود.": (None, "The employee was not found.", "未找到该员工。"),
    "اسم الموظف لا يمكن أن يكون فارغًا.": (None, "The employee name cannot be empty.", "员工姓名不能为空。"),
    "المسمى الوظيفي لا يمكن أن يكون فارغًا.": (None, "The job title cannot be empty.", "职位名称不能为空。"),
    "لا يمكن تسجيل تغيير في الراتب الأساسي بتاريخ سريان داخل شهر رواتب مقفل. اختر تاريخًا في الشهر الحالي، وسجّل الفرق كمكافأة أو خصم بأثر رجعي.": (None, "A basic salary change cannot be dated inside a closed payroll month. Choose a date in the current month and record the difference as a retroactive bonus or deduction.", "不能登记生效日期在已关闭工资月份内的基本工资变更。请选择当月日期，并将差额记为追溯奖金或扣款。"),
    "الموظف غير موجود أو غير نشط.": (None, "The employee was not found or is not active.", "未找到该员工或其未在职。"),
    "القسم الجديد غير متاح.": (None, "The new department is not available.", "新部门不可用。"),
    "الفرع الجديد غير صالح.": (None, "The new branch is not valid.", "新分支无效。"),
    "لا توجد فترة خدمة مفتوحة.": (None, "There is no open service period.", "没有未结束的服务期。"),
    "الموظف غير مؤرشف.": (None, "The employee is not archived.", "该员工未归档。"),
    "تاريخ إعادة التعيين غير صالح.": (None, "The rehire date is not valid.", "重新聘用日期无效。"),
    "سجل الإجازة غير موجود.": (None, "The leave record was not found.", "未找到该请假记录。"),
    "الحذف مسموح لمسودة الإجازة فقط؛ استخدم الإلغاء للسجلات الأخرى.": (None, "Only a draft leave can be deleted; use cancel for other records.", "只能删除草稿请假；其他记录请使用取消。"),
    "لا يمكن حذف إجازة لها حركة رصيد مرتبطة.": (None, "A leave with a linked balance entry cannot be deleted.", "已关联余额变动的请假不能删除。"),
    "الموظف أو نوع الإجازة غير موجود.": (None, "The employee or leave type was not found.", "未找到该员工或休假类型。"),
    "طلب السلفة غير موجود.": (None, "The advance request was not found.", "未找到该预支申请。"),
    "إجراء الموظف غير موجود.": (None, "The staff action was not found.", "未找到该员工事项。"),
    "سجل الحضور غير موجود.": (None, "The attendance record was not found.", "未找到该考勤记录。"),
    "مسير الرواتب غير موجود.": (None, "The payroll run was not found.", "未找到该工资单。"),
    "يجب إدخال وقت الدخول والخروج بصيغة تاريخ ووقت صحيحة.": (None, "Clock in and out must be valid date-times.", "上班和下班时间必须是有效的日期时间。"),
    "القسم المحدد غير متاح.": (None, "The selected department is not available.", "所选部门不可用。"),
    "تغير رصيد الإجازة بعد عرض المعاينة. أعد تجهيز الأمر وراجع القيم الجديدة.": (None, "The leave balance changed after the preview. Prepare the command again and review the new values.", "预览之后休假余额已变化。请重新准备指令并查看新数值。"),
    "الفرع المحدد غير تابع للقسم.": (None, "The selected branch does not belong to the department.", "所选分支不属于该部门。"),
    "بيانات الراتب أو ساعات العمل غير صحيحة.": (None, "The salary or working hours are invalid.", "工资或工时无效。"),
    # leave draft codes mapped in execute_plan
    "الموظف أو نوع الإجازة غير صالح.": (None, "The employee or leave type is not valid.", "员工或休假类型无效。"),
    "يوجد تداخل مع إجازة أخرى.": (None, "This overlaps with another leave.", "与另一条请假重叠。"),
    "بيانات الإجازة غير صحيحة.": (None, "The leave details are invalid.", "请假信息无效。"),
}

_REASONS[ERROR_MESSAGE] = (
    None, "The national ID must be 14 digits: a century digit, a valid birth date and a known governorate code.",
    "国民身份证号必须为14位：世纪位、有效的出生日期和已知的省份代码。")

# Parameterised refusals: strict patterns, only digits / short codes are captured.
_PATTERNS = [
    (re.compile(r"Monthly advance limit is ([0-9]{1,12}(?:\.[0-9]{1,4})?); already reserved: ([0-9]{1,12}(?:\.[0-9]{1,4})?)\.", re.ASCII),
     ("الحد الشهري للسلف {0}؛ المحجوز بالفعل: {1}.", "The monthly advance limit is {0}; already reserved: {1}.", "每月预支上限为 {0}；已预留：{1}。")),
    (re.compile(r"Payroll deductions exceed earnings for employee #([0-9]{1,10})\.", re.ASCII),
     ("الخصومات تتجاوز المستحقات للموظف رقم {0}.", "Payroll deductions exceed earnings for employee #{0}.", "员工 #{0} 的工资扣除超过应发金额。")),
    (re.compile(r"Advance deductions changed after payroll draft for employee #([0-9]{1,10}); reopen/recreate payroll before closing\.", re.ASCII),
     ("تغيرت خصومات السلف بعد مسودة الرواتب للموظف رقم {0}؛ أعد فتح المسير أو أنشئه من جديد قبل الإغلاق.",
      "Advance deductions changed after the payroll draft for employee #{0}; reopen or recreate payroll before closing.",
      "员工 #{0} 的预支扣除在工资草稿之后发生变化；关闭前请重新打开或重新生成工资单。")),
    (re.compile(r"Unreviewed absence/paid-leave conflicts: ([A-Za-z0-9_\-]{1,40}(?:, [A-Za-z0-9_\-]{1,40}){0,99})", re.ASCII),
     ("توجد تعارضات غياب/إجازة مدفوعة لم تُراجع: {0}.", "Unreviewed absence and paid-leave conflicts: {0}.", "存在未审核的缺勤与带薪假冲突：{0}。")),
    (re.compile(r"(national_id|fingerprint_id) مستخدم بالفعل\."),
     ("هذا الرقم مستخدم بالفعل لموظف آخر.", "This value is already used by another employee.", "该值已被其他员工使用。")),
    (re.compile(r"التاريخ [\u0600-\u06FF]{1,20}(?: [\u0600-\u06FF]{1,20})? غير صالح؛ استخدم YYYY-MM-DD\."),
     ("تاريخ غير صالح؛ استخدم YYYY-MM-DD.", "A date is not valid; use YYYY-MM-DD.", "日期无效；请使用 YYYY-MM-DD。")),
    (re.compile(r"القيمة [\u0600-\u06FF]{1,20}(?: [\u0600-\u06FF]{1,20})? غير رقمية\."),
     ("قيمة غير رقمية.", "A value is not a number.", "数值无效。")),
]

_MAX_TEXT = 400
_LANGS = {"ar": 0, "en": 1, "zh": 2}


def _pick(entry: tuple, key: str, language: str) -> str:
    value = entry[_LANGS.get(language, 0)]
    return key if value is None else value


def known_reason(exc: BaseException, language: str) -> str | None:
    """Localized reason for an exact known business refusal, else None."""
    if isinstance(exc, LocalizedMessage):
        # Not trusted because of the marker: exact class, one str arg, language == request language, and the text
        # equals a catalogued authored message for that language.
        if (type(exc) not in (LocalizedValueError, LocalizedRuntimeError) or len(exc.args) != 1
                or type(exc.args[0]) is not str or exc.language != language
                or not is_authored(exc.args[0], language) or str(exc) != exc.args[0]):
            return None
        return exc.args[0]
    if type(exc) not in (ValueError, RuntimeError, AbsenceLeaveConflictError) or len(exc.args) != 1 or type(exc.args[0]) is not str:
        return None
    if len(exc.args[0]) > _MAX_TEXT:
        return None
    text = exc.args[0]
    entry = _REASONS.get(text)
    if entry is not None:
        return _pick(entry, text, language)
    for pattern, templates in _PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            return templates[_LANGS.get(language, 0)].format(*match.groups())
    return None


def safe_failure_message(exc: BaseException, prefix: str, generic: str, language: str) -> str:
    """``prefix: reason`` for a known reason, otherwise the fixed generic message."""
    reason = known_reason(exc, language)
    if reason is None:
        return generic
    if isinstance(exc, LocalizedMessage):
        return reason  # authored as a complete sentence in the request language
    return f"{prefix}: {reason}"
