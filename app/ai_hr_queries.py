"""Read-only HR query reports for the local AI assistant.

These helpers never write to the database. They answer structured questions about
attendance review, leave balances, advances/installments, and staff actions.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select

from app.db import SessionLocal
from app.leave_workflow import leave_balance
from app.time_semantics import local_elapsed_minutes
from app.models import (
    AdvanceInstallment,
    AdvanceRequest,
    AttendanceDailyRecord,
    Employee,
    LeaveType,
    StaffAction,
    Department,
    Branch,
    PayrollRun,
    PayrollItem,
    PayrollPayment,
)

def _msg(language: str, ar: str, en: str, zh: str) -> str:
    return {"ar": ar, "en": en, "zh": zh}.get(language, ar)


_NOTE = {
    "ar": {
        "note_readonly_employee": "قراءة مباشرة من السجل المحلي. لم يتم تعديل أي بيانات.",
        "note_employee_not_found": "لم يتم العثور على الموظف المحدد؛ لم يتم عرض بيانات موظفين آخرين.",
        "note_readonly_attendance": "تقرير مراجعة فقط. لم يتم اعتماد أو رفض أي سجل حضور.",
        "note_no_leave_types": "لا توجد أنواع إجازات مفعّلة في النظام.",
        "note_specify_employee": "حدد الموظف بالرمز (مثل MY-0001) أو بالاسم لعرض أرصدة الإجازات.",
        "note_readonly_leave": "أرصدة محسوبة من سجل الحركات فقط (قراءة).",
        "note_readonly_advances": "ملخص سلف نشطة/مدفوعة غير مغلقة بالكامل. قراءة فقط.",
        "note_readonly_staff": "ملخص جزاءات/مكافآت للفترة. قراءة فقط ولم يُعتمد أي إجراء من هنا.",
    },
    "en": {
        "note_readonly_employee": "Direct read from the local register. No data was changed.",
        "note_employee_not_found": "The specified employee was not found; no other employees' data is shown.",
        "note_readonly_attendance": "Review-only report. No attendance record was approved or rejected.",
        "note_no_leave_types": "No active leave types in the system.",
        "note_specify_employee": "Specify the employee by code (e.g. MY-0001) or name to show leave balances.",
        "note_readonly_leave": "Balances computed from the transaction ledger only (read-only).",
        "note_readonly_advances": "Summary of open active/paid advances. Read-only.",
        "note_readonly_staff": "Sanctions/rewards summary for the period. Read-only; nothing was approved here.",
    },
    "zh": {
        "note_readonly_employee": "直接读取本地记录，未修改任何数据。",
        "note_employee_not_found": "未找到指定员工，未显示其他员工的数据。",
        "note_readonly_attendance": "仅供审阅的报告，未批准或拒绝任何考勤记录。",
        "note_no_leave_types": "系统中没有启用的请假类型。",
        "note_specify_employee": "请用编号（如 MY-0001）或姓名指定员工以显示请假余额。",
        "note_readonly_leave": "余额仅根据流水账计算（只读）。",
        "note_readonly_advances": "未结清的在职/已付预支汇总。只读。",
        "note_readonly_staff": "该期间奖惩汇总。只读，未在此批准任何操作。",
    },
}

_DATE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_MONTH_RE = re.compile(r"(20\d{2})-(\d{2})")
_CODE_RE = re.compile(r"\b(MY[-\s]?\d{3,6}|SEC[-\s]?\d{1,4})\b", re.IGNORECASE)


def _parse_date(question: str, default: date | None = None) -> date:
    match = _DATE_RE.search(question or "")
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return default or date.today()


def _parse_month(question: str, default: date | None = None) -> date:
    """Return first day of month from YYYY-MM or YYYY-MM-DD."""
    full = _DATE_RE.search(question or "")
    if full:
        return date(int(full.group(1)), int(full.group(2)), 1)
    month = _MONTH_RE.search(question or "")
    if month:
        return date(int(month.group(1)), int(month.group(2)), 1)
    base = default or date.today()
    return date(base.year, base.month, 1)


def _month_end(month_start: date) -> date:
    if month_start.month == 12:
        return date(month_start.year + 1, 1, 1) - timedelta(days=1)
    return date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)


def _employee_filter_requested(question: str) -> bool:
    """True only when the user names a specific employee, not a plural group."""
    q = " ".join((question or "").casefold().split())
    if _CODE_RE.search(q):
        return True
    return bool(re.search(
        r"(?:للموظف|الموظف|موظف|للحارس|الحارس|حارس)\s+\S",
        q,
    ))


def _find_employees(db, question: str) -> list[Employee]:
    """Resolve employees by code or an explicit singular name reference."""
    code_match = _CODE_RE.search(question or "")
    if code_match:
        raw = re.sub(r"\s+", "", code_match.group(1)).upper()
        if raw.startswith("MY") and not raw.startswith("MY-"):
            raw = "MY-" + raw[2:]
        if raw.startswith("SEC") and not raw.startswith("SEC-"):
            raw = "SEC-" + raw[3:]
        return list(db.scalars(select(Employee).where(Employee.code == raw)).all())

    q = " ".join((question or "").split())
    match = re.search(
        r"(?:للموظف|الموظف|موظف|للحارس|الحارس|حارس)\s+(.+)",
        q,
    )
    if not match:
        return []
    tail = match.group(1).strip(" :：-")
    tail = re.split(r"\d{4}-\d{2}", tail)[0].strip(" ؟?.,")
    if not tail or len(tail) < 2:
        return []
    rows = db.scalars(
        select(Employee).where(Employee.full_name.ilike(f"%{tail}%")).limit(5)
    ).all()
    return list(rows)


def is_attendance_analysis_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    if not q:
        return False
    attendance = any(term in q for term in ("حضور", "انصراف", "تأخير", "غياب", "attendance", "考勤", "出勤", "迟到"))
    analysis = any(
        term in q
        for term in (
            "تحليل", "حلّل", "حلل", "ملخص", "ملاحظات", "مراجعة",
            "مشاكل", "تقرير", "summary", "analyze", "分析", "汇总", "问题",
        )
    )
    if "أمن" in q and "بصم" in q:
        return False
    return attendance and analysis


def is_leave_balance_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    return any(term in q for term in (
        "رصيد إجازة", "رصيد اجازة", "أرصدة الإجازات", "ارصدة الاجازات", "إجازات الموظف", "اجازات الموظف",
        "leave balance", "رصيد", "请假余额", "假期余额", "年假余额",
    ))


def is_advance_query_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    return any(term in q for term in (
        "سلف", "سلفة", "أقساط", "اقساط", "قسط", "advance", "installment", "预支", "借款", "分期",
    ))


def is_staff_action_query_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    return any(
        term in q
        for term in ("جزاء", "جزاءات", "مكافأة", "مكافات", "مكافآت", "عقوبة", "خصم تأديبي",
                     "sanction", "reward", "penalty", "奖惩", "处罚", "奖励")
    )


def is_employee_query_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    return any(term in q for term in (
        "بيانات الموظف", "تفاصيل الموظف", "سجل الموظف", "ابحث عن الموظف",
        "بيانات العامل", "employee profile", "employee details",
        "员工信息", "员工详情", "员工资料",
    ))


def is_payroll_query_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    return any(term in q for term in (
        "راتب الموظف", "رواتب الموظفين", "مسير الرواتب", "تفاصيل الراتب",
        "صافي الراتب", "payroll", "salary details",
        "工资批次", "工资明细", "薪资", "实发工资",
    ))


def build_employee_report(question: str) -> dict[str, Any]:
    with SessionLocal() as db:
        employees = _find_employees(db, question)
        items = []
        for employee in employees:
            department = db.get(Department, employee.department_id)
            branch = db.get(Branch, employee.branch_id) if employee.branch_id else None
            items.append({
                "id": employee.id, "code": employee.code, "name": employee.full_name,
                "status": employee.status, "department": department.name if department else "",
                "branch": branch.name if branch else "—", "position": employee.position,
                "hire_date": employee.hire_date.isoformat(), "basic_salary": str(employee.basic_salary),
                "daily_hours": str(employee.daily_hours),
            })
    return {"items": items, "note": "note_readonly_employee"}


def format_employee_report(report: dict[str, Any], language: str = "ar") -> str:
    lines = [
        _msg(language, "نتيجة البحث في سجل الموظفين.", "Employee register search result.", "员工登记查询结果。"),
        _NOTE[language if language in _NOTE else "ar"][report.get("note", "note_readonly_employee")],
    ]
    if not report.get("items"):
        lines.append(_msg(language,
            "لم أعثر على موظف مطابق. استخدم كود الموظف MY-0001 أو الاسم المسجل.",
            "No matching employee found. Use the employee code MY-0001 or the registered name.",
            "未找到匹配的员工。请使用员工编号 MY-0001 或登记姓名。"))
        return "\n".join(lines)
    not_set = _msg(language, "غير محدد", "Not set", "未设定")
    for item in report["items"]:
        lines.append(
            f"{item['code']} — {item['name']} — {item['status']} — "
            f"{item['department'] or not_set} / {item['branch']} — {item['position']} — "
            + _msg(language,
                f"تعيين {item['hire_date']} — أساسي {item['basic_salary']} جنيه — {item['daily_hours']} ساعة يوميًا",
                f"hired {item['hire_date']} — base {item['basic_salary']} EGP — {item['daily_hours']} h/day",
                f"入职 {item['hire_date']} — 基本工资 {item['basic_salary']} 埃镑 — 每天 {item['daily_hours']} 小时")
        )
    lines.append(_msg(language, "المصدر: جدول الموظفين المحلي.", "Source: local employees table.", "来源：本地员工表。"))
    return "\n".join(lines)


def format_employee_report_ar(report: dict[str, Any]) -> str:
    return format_employee_report(report, "ar")


def build_payroll_report(question: str) -> dict[str, Any]:
    month = _parse_month(question)
    with SessionLocal() as db:
        run = db.scalar(select(PayrollRun).where(PayrollRun.period_start == month))
        if run is None:
            return {"month": month.isoformat(), "status": "not_created", "items": []}
        employees = _find_employees(db, question)
        employee_ids = {employee.id for employee in employees}
        if _employee_filter_requested(question) and not employee_ids:
            return {
                "month": month.isoformat(), "status": run.status,
                "pay_date": run.pay_date.isoformat(), "items": [],
                "note": "note_employee_not_found",
            }
        query = select(PayrollItem).where(PayrollItem.payroll_run_id == run.id)
        if employee_ids:
            query = query.where(PayrollItem.employee_id.in_(employee_ids))
        items = db.scalars(query.order_by(PayrollItem.employee_code_snapshot)).all()
        rows = []
        for item in items:
            paid = db.scalar(select(func.coalesce(func.sum(PayrollPayment.amount), 0)).where(
                PayrollPayment.payroll_item_id == item.id
            ))
            rows.append({
                "code": item.employee_code_snapshot, "name": item.employee_name_snapshot,
                "base_salary": str(item.base_salary), "bonuses": str(
                    item.attendance_bonus + item.performance_bonus + item.occupancy_bonus
                    + item.overtime_amount + item.other_bonus + item.staff_reward_amount
                ),
                "deductions": str(
                    item.absence_deduction + item.lateness_deduction + item.insurance_share
                    + item.taxes + item.other_deductions + item.advance_deduction
                    + item.staff_sanction_amount
                ),
                "net_pay": str(item.net_pay), "paid": str(paid or 0),
                "status": run.status,
            })
        return {"month": month.isoformat(), "status": run.status, "pay_date": run.pay_date.isoformat(),
                "items": rows}


def format_payroll_report(report: dict[str, Any], language: str = "ar") -> str:
    if report.get("status") == "not_created":
        return _msg(language,
            f"لا يوجد مسير رواتب منشأ لشهر {report['month']}.",
            f"No payroll run created for {report['month']}.",
            f"{report['month']} 尚未创建工资批次。")
    lines = [_msg(language,
        f"ملخص مسير الرواتب لشهر {report['month']} — الحالة: {report['status']} — موعد الصرف: {report['pay_date']}.",
        f"Payroll run summary for {report['month']} — status: {report['status']} — pay date: {report['pay_date']}.",
        f"{report['month']} 工资批次摘要 — 状态：{report['status']} — 发放日期：{report['pay_date']}。")]
    if report.get("note"):
        lines.append(_NOTE[language if language in _NOTE else "ar"][report["note"]])
    if not report.get("items"):
        lines.append(_msg(language,
            "لا توجد بنود مطابقة للموظف أو الفترة المحددة.",
            "No items match the selected employee or period.",
            "没有匹配所选员工或期间的条目。"))
        return "\n".join(lines)
    for item in report["items"]:
        lines.append(
            f"{item['code']} — {item['name']} — " + _msg(language,
                f"أساسي {item['base_salary']} — إضافات {item['bonuses']} — خصومات {item['deductions']} — صافي {item['net_pay']} — مدفوع {item['paid']}",
                f"base {item['base_salary']} — additions {item['bonuses']} — deductions {item['deductions']} — net {item['net_pay']} — paid {item['paid']}",
                f"基本 {item['base_salary']} — 加项 {item['bonuses']} — 扣款 {item['deductions']} — 实发 {item['net_pay']} — 已付 {item['paid']}")
        )
    lines.append(_msg(language,
        f"المصدر: مسير رواتب {report['month']} وبنوده ومدفوعاته.",
        f"Source: payroll run {report['month']}, its items and payments.",
        f"来源：{report['month']} 工资批次及其条目和付款记录。"))
    return "\n".join(lines)


def format_payroll_report_ar(report: dict[str, Any]) -> str:
    return format_payroll_report(report, "ar")


def is_structured_hr_query(question: str) -> bool:
    return (
        is_employee_query_request(question)
        or is_payroll_query_request(question)
        or is_attendance_analysis_request(question)
        or is_leave_balance_request(question)
        or is_advance_query_request(question)
        or is_staff_action_query_request(question)
    )


def _classify_attendance_row(record: AttendanceDailyRecord) -> list[tuple[str, object]]:
    """Structured flags; localized at display time, never stored."""
    notes: list[tuple[str, object]] = []
    if record.status in {"needs_review", "ready_for_review"}:
        notes.append(("record_status", record.status))
    # Late/early mirror payroll's rule exactly: naive wall-clock subtraction
    # (payroll_workflow computes deductions the same way), so the assistant
    # never contradicts a payroll figure.
    if record.scheduled_start_at and record.first_clock_in:
        late = max(0, int((record.first_clock_in - record.scheduled_start_at).total_seconds() // 60))
        if late > 0:
            notes.append(("late_in", late))
    if record.scheduled_end_at and record.last_clock_out:
        early = max(0, int((record.scheduled_end_at - record.last_clock_out).total_seconds() // 60))
        if early > 0:
            notes.append(("early_out", early))
    if record.first_clock_in is None and record.program_clock_in is None:
        notes.append(("no_clock_in", None))
    if record.last_clock_out is None and record.program_clock_out is None:
        notes.append(("no_clock_out", None))
    if record.worked_minutes is not None and record.scheduled_start_at and record.scheduled_end_at:
        # worked_minutes is stored as true elapsed time, so the scheduled side
        # must use the same basis: a plain wall-clock subtraction would false-
        # flag a perfect shift on a DST transition night.
        scheduled = local_elapsed_minutes(record.scheduled_start_at, record.scheduled_end_at)
        if scheduled > 0 and abs(record.worked_minutes - scheduled) >= 60:
            notes.append(("unexpected_duration", (record.worked_minutes, scheduled)))
    if record.review_notes:
        notes.append(("saved_note", record.review_notes))
    return notes


def _format_attendance_flag(flag: tuple[str, object], language: str) -> str:
    key, value = flag
    if key == "record_status":
        return _msg(language, f"حالة السجل: {value}", f"Record status: {value}", f"记录状态：{value}")
    if key == "late_in":
        return _msg(language, f"تأخير حضور {value} دقيقة", f"Late clock-in by {value} minutes", f"上班迟到 {value} 分钟")
    if key == "early_out":
        return _msg(language, f"انصراف مبكر {value} دقيقة", f"Early clock-out by {value} minutes", f"下班早退 {value} 分钟")
    if key == "no_clock_in":
        return _msg(language, "لا توجد بصمة/تسجيل دخول", "No clock-in punch/record", "无上班打卡/记录")
    if key == "no_clock_out":
        return _msg(language, "لا توجد بصمة/تسجيل انصراف", "No clock-out punch/record", "无下班打卡/记录")
    if key == "unexpected_duration":
        worked, scheduled = value  # type: ignore[misc]
        return _msg(language,
            f"مدة عمل غير متوقعة ({worked} دقيقة مقابل {scheduled} مجدولة)",
            f"Unexpected work duration ({worked} minutes vs {scheduled} scheduled)",
            f"工作时长异常（实际 {worked} 分钟，计划 {scheduled} 分钟）")
    return _msg(language, f"ملاحظة محفوظة: {value}", f"Saved note: {value}", f"已存备注：{value}")


def build_attendance_analysis(*, work_date: date | None, month_start: date | None) -> dict[str, Any]:
    with SessionLocal() as db:
        query = select(AttendanceDailyRecord, Employee).join(
            Employee, Employee.id == AttendanceDailyRecord.employee_id
        )
        if work_date is not None:
            query = query.where(AttendanceDailyRecord.work_date == work_date)
            period_label = work_date.isoformat()
        else:
            assert month_start is not None
            end = _month_end(month_start)
            query = query.where(
                AttendanceDailyRecord.work_date >= month_start,
                AttendanceDailyRecord.work_date <= end,
            )
            period_label = f"{month_start.isoformat()} → {end.isoformat()}"

        rows = db.execute(query.order_by(AttendanceDailyRecord.work_date, Employee.code)).all()
        items: list[dict[str, Any]] = []
        for record, employee in rows:
            flags = _classify_attendance_row(record)
            if not flags and record.status == "approved":
                continue
            if not flags:
                continue
            items.append({
                "employee_code": employee.code,
                "employee_name": employee.full_name,
                "work_date": record.work_date.isoformat(),
                "status": record.status,
                "notes": flags,
            })
        return {
            "period": period_label,
            "reviewed_rows": len(rows),
            "flagged_rows": len(items),
            "items": items,
            "note": "note_readonly_attendance",
        }


def format_attendance_analysis(report: dict[str, Any], language: str = "ar") -> str:
    lang = language if language in _NOTE else "ar"
    lines = [
        _msg(language, f"تحليل حضور للفترة {report['period']}.",
             f"Attendance analysis for {report['period']}.", f"{report['period']} 考勤分析。"),
        _NOTE[lang][report.get("note", "note_readonly_attendance")],
        _msg(language,
             f"سجلات مفحوصة: {report['reviewed_rows']} — تحتاج انتباه: {report['flagged_rows']}.",
             f"Rows checked: {report['reviewed_rows']} — need attention: {report['flagged_rows']}.",
             f"已检查记录：{report['reviewed_rows']} — 需要注意：{report['flagged_rows']}。"),
    ]
    if not report["items"]:
        lines.append(_msg(language,
            "لا توجد ملاحظات ظاهرة في السجلات المتاحة لهذه الفترة.",
            "No visible issues in the available records for this period.",
            "该期间现有记录中没有明显问题。"))
        return "\n".join(lines)
    lines.append(_msg(language, "التفاصيل:", "Details:", "明细："))
    for index, item in enumerate(report["items"][:40], start=1):
        lines.append(
            f"{index}) {item['employee_code']} — {item['employee_name']} — {item['work_date']} — {item['status']}"
        )
        for flag in item["notes"]:
            lines.append(f"   - {_format_attendance_flag(flag, language)}")
    if report["flagged_rows"] > 40:
        lines.append(_msg(language,
            f"... و{report['flagged_rows'] - 40} سجلًا إضافيًا. راجع شاشة الحضور للتفاصيل الكاملة.",
            f"... and {report['flagged_rows'] - 40} more records. See the attendance screen for full details.",
            f"……另有 {report['flagged_rows'] - 40} 条记录。完整明细请查看考勤页面。"))
    lines.append(_msg(language,
        f"المصدر: سجلات الحضور اليومية للفترة {report['period']}.",
        f"Source: daily attendance records for {report['period']}.",
        f"来源：{report['period']} 期间的每日考勤记录。"))
    return "\n".join(lines)


def format_attendance_analysis_ar(report: dict[str, Any]) -> str:
    return format_attendance_analysis(report, "ar")


def build_leave_balance_report(question: str) -> dict[str, Any]:
    with SessionLocal() as db:
        employees = _find_employees(db, question)
        types = db.scalars(
            select(LeaveType).where(LeaveType.active.is_(True)).order_by(LeaveType.id)
        ).all()
        if not types:
            return {"employees": [], "note": "note_no_leave_types"}
        if not employees:
            return {
                "employees": [],
                "note": "note_specify_employee",
            }
        result = []
        for employee in employees:
            balances = []
            for leave_type in types:
                if not leave_type.tracks_balance:
                    continue
                bal = leave_balance(db, employee.id, leave_type.id)
                balances.append({
                    "leave_type_ar": leave_type.name_ar or leave_type.code,
                    "leave_type_en": leave_type.name_en or "",
                    "leave_type_zh": getattr(leave_type, "name_zh", None) or "",
                    "days": str(bal),
                })
            result.append({
                "code": employee.code,
                "name": employee.full_name,
                "balances": balances,
            })
        return {"employees": result, "note": "note_readonly_leave"}


def format_leave_balance(report: dict[str, Any], language: str = "ar") -> str:
    lang = language if language in _NOTE else "ar"
    lines = [_NOTE[lang][report.get("note", "note_readonly_leave")]]
    if not report.get("employees"):
        return lines[0]
    day_word = _msg(language, "يوم", "days", "天")
    for emp in report["employees"]:
        lines.append(_msg(language, f"الموظف {emp['code']} — {emp['name']}:",
                          f"Employee {emp['code']} — {emp['name']}:",
                          f"员工 {emp['code']} — {emp['name']}："))
        if not emp["balances"]:
            lines.append(_msg(language, "   لا توجد أنواع إجازة تتابع رصيدًا.",
                              "   No balance-tracking leave types.", "   没有跟踪余额的请假类型。"))
            continue
        for item in emp["balances"]:
            if language == "ar":
                type_name = item["leave_type_ar"]
            elif language == "zh":
                type_name = item["leave_type_zh"] or item["leave_type_en"] or item["leave_type_ar"]
            else:
                type_name = item["leave_type_en"] or item["leave_type_ar"]
            lines.append(f"   - {type_name}: {item['days']} {day_word}")
    lines.append(_msg(language, "المصدر: سجل حركات أرصدة الإجازات.",
                      "Source: leave balance transaction ledger.", "来源：请假余额流水账。"))
    return "\n".join(line for line in lines if line)


def format_leave_balance_ar(report: dict[str, Any]) -> str:
    return format_leave_balance(report, "ar")


def build_advance_report(question: str) -> dict[str, Any]:
    with SessionLocal() as db:
        employees = _find_employees(db, question)
        if _employee_filter_requested(question) and not employees:
            return {
                "items": [],
                "note": "note_employee_not_found",
            }
        query = select(AdvanceRequest, Employee).join(
            Employee, Employee.id == AdvanceRequest.employee_id
        ).where(AdvanceRequest.status.in_(["pending", "approved", "paid"]))
        if employees:
            ids = [item.id for item in employees]
            query = query.where(AdvanceRequest.employee_id.in_(ids))
        rows = db.execute(query.order_by(AdvanceRequest.id.desc()).limit(30)).all()
        items = []
        for request, employee in rows:
            installments = db.scalars(
                select(AdvanceInstallment).where(
                    AdvanceInstallment.advance_request_id == request.id
                ).order_by(AdvanceInstallment.installment_number)
            ).all()
            remaining = [
                inst for inst in installments
                if inst.status in {"scheduled", "reversed"}
            ]
            remaining_amount = sum((inst.amount for inst in remaining), Decimal("0"))
            items.append({
                "id": request.id,
                "code": employee.code,
                "name": employee.full_name,
                "amount": str(request.amount),
                "type": request.advance_type,
                "status": request.status,
                "remaining_installments": len(remaining),
                "remaining_amount": str(remaining_amount.quantize(Decimal("0.01"))),
            })
        return {
            "items": items,
            "note": "note_readonly_advances",
        }


def format_advance_report(report: dict[str, Any], language: str = "ar") -> str:
    lang = language if language in _NOTE else "ar"
    lines = [_NOTE[lang][report.get("note", "note_readonly_advances")]]
    if not report.get("items"):
        lines.append(_msg(language, "لا توجد سلف مطابقة للبحث.", "No advances match the search.", "没有匹配的预支记录。"))
        return "\n".join(lines)
    for item in report["items"]:
        lines.append(
            f"#{item['id']} — {item['code']} {item['name']} — " + _msg(language,
                f"مبلغ {item['amount']} — نوع {item['type']} — حالة {item['status']} — أقساط متبقية {item['remaining_installments']} بقيمة {item['remaining_amount']}",
                f"amount {item['amount']} — type {item['type']} — status {item['status']} — {item['remaining_installments']} remaining installments totaling {item['remaining_amount']}",
                f"金额 {item['amount']} — 类型 {item['type']} — 状态 {item['status']} — 剩余 {item['remaining_installments']} 期，共 {item['remaining_amount']}")
        )
    lines.append(_msg(language, "المصدر: طلبات السلف وجداول أقساطها.",
                      "Source: advance requests and their installment schedules.", "来源：预支申请及其分期计划。"))
    return "\n".join(lines)


def format_advance_report_ar(report: dict[str, Any]) -> str:
    return format_advance_report(report, "ar")


def build_staff_actions_report(question: str) -> dict[str, Any]:
    month_start = _parse_month(question)
    month_end = _month_end(month_start)
    with SessionLocal() as db:
        employees = _find_employees(db, question)
        if _employee_filter_requested(question) and not employees:
            return {
                "month": month_start.isoformat()[:7], "items": [],
                "note": "note_employee_not_found",
            }
        query = select(StaffAction, Employee).join(
            Employee, Employee.id == StaffAction.employee_id
        )
        query = query.where(
            or_(
                (StaffAction.payroll_month >= month_start) & (StaffAction.payroll_month <= month_end),
                (StaffAction.payroll_month.is_(None))
                & (func.date(StaffAction.created_at) >= month_start)
                & (func.date(StaffAction.created_at) <= month_end),
            )
        )
        if employees:
            query = query.where(StaffAction.employee_id.in_([e.id for e in employees]))
        rows = db.execute(query.order_by(StaffAction.id.desc()).limit(40)).all()
        items = []
        for action, employee in rows:
            items.append({
                "id": action.id,
                "code": employee.code,
                "name": employee.full_name,
                "kind": action.kind,
                "title": action.title,
                "status": action.status,
                "amount": str(action.amount) if action.amount is not None else None,
                "payroll_month": action.payroll_month.isoformat() if action.payroll_month else None,
            })
        return {
            "month": month_start.isoformat()[:7],
            "items": items,
            "note": "note_readonly_staff",
        }


def format_staff_actions(report: dict[str, Any], language: str = "ar") -> str:
    lang = language if language in _NOTE else "ar"
    lines = [
        _msg(language, f"جزاءات ومكافآت لشهر {report['month']}.",
             f"Sanctions and rewards for {report['month']}.", f"{report['month']} 奖惩汇总。"),
        _NOTE[lang][report.get("note", "note_readonly_staff")],
    ]
    if not report.get("items"):
        lines.append(_msg(language, "لا توجد سجلات مطابقة.", "No matching records.", "没有匹配的记录。"))
        return "\n".join(lines)
    for item in report["items"]:
        amount = _msg(language, f" — مبلغ {item['amount']}", f" — amount {item['amount']}", f" — 金额 {item['amount']}") if item["amount"] else ""
        lines.append(
            f"#{item['id']} — {item['code']} {item['name']} — {item['kind']} — "
            f"{item['title']} — {item['status']}{amount}"
        )
    lines.append(_msg(language, f"المصدر: سجل الجزاءات والمكافآت لشهر {report['month']}.",
                      f"Source: sanctions and rewards register for {report['month']}.",
                      f"来源：{report['month']} 奖惩记录。"))
    return "\n".join(lines)


def format_staff_actions_ar(report: dict[str, Any]) -> str:
    return format_staff_actions(report, "ar")


def answer_structured_hr_query(question: str, language: str = "ar") -> str:
    """Dispatch a natural-language HR question to the matching read-only report."""
    if is_employee_query_request(question):
        return format_employee_report(build_employee_report(question), language)
    if is_payroll_query_request(question):
        return format_payroll_report(build_payroll_report(question), language)
    if is_attendance_analysis_request(question):
        q = question or ""
        if _DATE_RE.search(q) and "شهر" not in q:
            report = build_attendance_analysis(work_date=_parse_date(q), month_start=None)
        else:
            report = build_attendance_analysis(work_date=None, month_start=_parse_month(q))
        return format_attendance_analysis(report, language)
    if is_leave_balance_request(question):
        return format_leave_balance(build_leave_balance_report(question), language)
    if is_advance_query_request(question):
        return format_advance_report(build_advance_report(question), language)
    if is_staff_action_query_request(question):
        return format_staff_actions(build_staff_actions_report(question), language)
    return _msg(language,
        "لم أتعرّف على نوع الاستعلام. جرّب: تحليل حضور، رصيد إجازة، سلف، أو جزاءات.",
        "I could not recognize the query type. Try: attendance analysis, leave balance, advances, or sanctions.",
        "无法识别查询类型。请尝试：考勤分析、请假余额、预支或奖惩。")
