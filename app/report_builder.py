"""Custom HR report builder: selectable content, department-grouped preview, matching exports.

The approved reports redesign lets the HR administrator pick exactly which data appears
in a report. This module resolves a report selection (type, period, scope, fields) into
a department-grouped result with a summary, a comparison against the previous equivalent
period, and data-gap warnings, then renders the same selection as Excel or PDF.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CAIRO_TZ
from app.xlsx_safety import neutralize_formula_cells
from app.models import (
    AdvanceRequest,
    AttendanceDailyRecord,
    Department,
    Employee,
    LeaveRecord,
    LeaveType,
    PayrollItem,
    PayrollRun,
    ReportPreset,
    StaffAction,
)

REPORT_TYPE_PAYROLL = "payroll"
REPORT_TYPE_ATTENDANCE = "attendance"
REPORT_TYPE_ADVANCES = "advances"
REPORT_TYPE_LEAVE = "leave"
REPORT_TYPE_STAFF_ACTIONS = "staff_actions"
REPORT_TYPE_COMPREHENSIVE = "comprehensive"
REPORT_TYPES = [
    REPORT_TYPE_PAYROLL,
    REPORT_TYPE_ATTENDANCE,
    REPORT_TYPE_ADVANCES,
    REPORT_TYPE_LEAVE,
    REPORT_TYPE_STAFF_ACTIONS,
    REPORT_TYPE_COMPREHENSIVE,
]
REPORT_TYPE_LABELS = {
    REPORT_TYPE_PAYROLL: "رواتب",
    REPORT_TYPE_ATTENDANCE: "حضور وانصراف",
    REPORT_TYPE_ADVANCES: "سلف",
    REPORT_TYPE_LEAVE: "إجازات",
    REPORT_TYPE_STAFF_ACTIONS: "جزاءات ومكافآت",
    REPORT_TYPE_COMPREHENSIVE: "شامل",
}

EMPLOYEE_FIELD_DEFS = [
    ("emp_name", "اسم الموظف"),
    ("emp_code", "رقم الموظف"),
    ("emp_department", "القسم"),
    ("emp_nationality", "الجنسية (مصري / أجنبي)"),
    ("emp_id_proof", "إثبات الهوية — الرقم القومي للمصري، الباسبور للأجنبي"),
    ("emp_hire_date", "تاريخ التعيين"),
]
PAYROLL_FIELD_DEFS = [
    ("pay_basic", "الأساسي"),
    ("pay_allowances", "البدلات والحوافز"),
    ("pay_deductions", "الخصومات والجزاءات"),
    ("pay_advances", "السلف المخصومة"),
    ("pay_net", "صافي المرتب"),
    ("pay_work_days", "عدد أيام العمل"),
]
ATTENDANCE_FIELD_DEFS = [
    ("att_present", "أيام الحضور"),
    ("att_absent", "أيام الغياب"),
    ("att_late", "التأخير (بالدقائق)"),
    ("att_overtime", "الإضافي (بالساعات)"),
]
FIELD_LABELS = dict(EMPLOYEE_FIELD_DEFS + PAYROLL_FIELD_DEFS + ATTENDANCE_FIELD_DEFS)
ALL_FIELD_KEYS = set(FIELD_LABELS)
NUMERIC_FIELDS = {
    "pay_basic",
    "pay_allowances",
    "pay_deductions",
    "pay_advances",
    "pay_net",
    "pay_work_days",
    "att_present",
    "att_absent",
    "att_late",
    "att_overtime",
}
DEFAULT_FIELDS = {
    "emp_name",
    "emp_code",
    "emp_department",
    "emp_nationality",
    "emp_id_proof",
    "pay_basic",
    "pay_allowances",
    "pay_deductions",
    "pay_advances",
    "pay_net",
}
# Field groups offered per report type; the payroll report matches the approved design.
FIELD_GROUPS_BY_TYPE = {
    REPORT_TYPE_PAYROLL: ["employee", "payroll", "attendance"],
    REPORT_TYPE_ATTENDANCE: ["employee", "attendance"],
    REPORT_TYPE_ADVANCES: ["employee"],
    REPORT_TYPE_LEAVE: ["employee"],
    REPORT_TYPE_STAFF_ACTIONS: ["employee"],
    REPORT_TYPE_COMPREHENSIVE: ["employee"],
}
FIELD_GROUP_DEFS = {
    "employee": ("بيانات الموظف", EMPLOYEE_FIELD_DEFS),
    "payroll": ("الرواتب", PAYROLL_FIELD_DEFS),
    "attendance": ("الحضور", ATTENDANCE_FIELD_DEFS),
}

PERIOD_CHOICES = ["this_month", "last_month", "last_3_months", "custom"]
STATUS_CHOICES = ["active", "all", "archived"]

ADVANCE_STATUS_LABELS = {
    "pending": "معلقة",
    "approved": "معتمدة",
    "paid": "مصروفة",
    "rejected": "مرفوضة",
    "deducted": "مخصومة",
}
LEAVE_STATUS_LABELS = {"draft": "مسودة", "confirmed": "مؤكدة", "cancelled": "ملغاة"}
ACTION_STATUS_LABELS = {"draft": "مسودة", "approved": "معتمد", "cancelled": "ملغي"}
ACTION_KIND_LABELS = {"sanction": "جزاء", "reward": "مكافأة"}

_AR_MONTHS = [
    "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
]


def month_label(value: date) -> str:
    return f"{_AR_MONTHS[value.month - 1]} {value.year}"


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + (value.month - 1) + months
    return date(month_index // 12, month_index % 12 + 1, 1)


@dataclass
class ReportSelection:
    report_type: str = REPORT_TYPE_PAYROLL
    start_date: date | None = None
    end_date: date | None = None
    period_choice: str = "this_month"
    department_id: int | None = None
    employee_id: int | None = None
    employee_status: str = "active"
    compare_like_for_like: bool = True
    # Monthly-PDF only (owner decision 2026-11-23): payroll and attendance
    # sections show approved data only, so summary, details and charts agree.
    # Custom reports keep the default False and are unchanged.
    approved_only: bool = False
    # Monthly-PDF only (owner decision 2026-11-23): a leave spanning months
    # shows each month's own calendar days in that month, and charged days
    # count once in the month the leave starts. Custom reports keep the
    # default False and are unchanged.
    monthly_leave_clipping: bool = False
    monthly_leave_status: str | None = None
    monthly_leave_language: str | None = None
    # Monthly-PDF only: a full-calendar-month section compares against the
    # previous calendar month. The equal-length shift used by custom reports
    # would end inside the month before last for 31-day months and catch that
    # run too, since payroll items match runs by overlap. Custom reports keep
    # the default False and are unchanged.
    monthly_calendar_comparison: bool = False
    fields: set[str] = field(default_factory=lambda: set(DEFAULT_FIELDS))

    def to_config(self) -> dict:
        return {
            "report_type": self.report_type,
            "period_choice": self.period_choice,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "department_id": self.department_id,
            "employee_id": self.employee_id,
            "employee_status": self.employee_status,
            "compare_like_for_like": self.compare_like_for_like,
            "fields": sorted(self.fields),
        }

    @classmethod
    def from_config(cls, config: dict) -> "ReportSelection":
        selection = cls()
        apply_config(selection, config)
        return selection


def _parse_int(value) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def apply_config(selection: ReportSelection, config: dict) -> None:
    """Apply stored or submitted parameters onto a selection, validating every value."""
    report_type = str(config.get("report_type") or "").strip()
    if report_type in REPORT_TYPES:
        selection.report_type = report_type
    period_choice = str(config.get("period_choice") or "").strip()
    if period_choice in PERIOD_CHOICES:
        selection.period_choice = period_choice
    start = _parse_date(config.get("start_date"))
    end = _parse_date(config.get("end_date"))
    if start is not None:
        selection.start_date = start
    if end is not None:
        selection.end_date = end
    if "department_id" in config:
        selection.department_id = _parse_int(config.get("department_id"))
    if "employee_id" in config:
        selection.employee_id = _parse_int(config.get("employee_id"))
    status = str(config.get("employee_status") or "").strip()
    if status in STATUS_CHOICES:
        selection.employee_status = status
    if "compare_like_for_like" in config:
        selection.compare_like_for_like = bool(config.get("compare_like_for_like"))
    fields = config.get("fields")
    if isinstance(fields, (list, tuple, set)) and fields:
        chosen = {str(item) for item in fields} & ALL_FIELD_KEYS
        if chosen:
            selection.fields = chosen


def resolve_period(selection: ReportSelection, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    if selection.period_choice == "last_month":
        first = _add_months(_month_start(today), -1)
        return first, _month_start(today) - timedelta(days=1)
    if selection.period_choice == "last_3_months":
        return _add_months(_month_start(today), -2), today
    if selection.period_choice == "custom":
        start = selection.start_date or _month_start(today)
        end = selection.end_date or today
        if end < start:
            raise ValueError("تاريخ النهاية قبل تاريخ البداية.")
        return start, end
    return _month_start(today), today


def selection_from_params(params) -> ReportSelection:
    """Build a selection from request query parameters."""
    selection = ReportSelection()
    config = {
        "report_type": params.get("report_type"),
        "period_choice": params.get("period"),
        "start_date": params.get("start_date"),
        "end_date": params.get("end_date"),
        "department_id": params.get("department_id"),
        "employee_id": params.get("employee_id"),
        "employee_status": params.get("employee_status"),
    }
    if hasattr(params, "getlist"):
        fields = params.getlist("fields")
    else:
        fields = params.get("fields")
    config["fields"] = fields
    apply_config(selection, config)
    selection.compare_like_for_like = params.get("lfl") in ("1", "on", "true", "yes")
    # Explicit dates without an explicit period choice mean a custom range.
    if not params.get("period") and (params.get("start_date") or params.get("end_date")):
        selection.period_choice = "custom"
    return selection


def preset_config_from_params(params) -> dict:
    selection = selection_from_params(params)
    return selection.to_config()


def _previous_period(start: date, end: date) -> tuple[date, date]:
    """The custom report's original comparison: a plain equal-length shift.

    Owner constraint: the custom report flow is unchanged. The monthly-PDF
    wrapper uses _previous_calendar_month below for its own bounds instead."""
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    return prev_end - timedelta(days=length - 1), prev_end


def _previous_calendar_month(start: date, end: date) -> tuple[date, date]:
    """Monthly-PDF comparison only: a full calendar month compares against the
    previous calendar month, since the payroll lookup matches runs by overlap
    and an equal-length shift (ending 31/08 for an October period) would also
    catch the August run. Other ranges keep the equal-length shift."""
    if start.day == 1 and _add_months(start, 1) - timedelta(days=1) == end:
        return _add_months(start, -1), start - timedelta(days=1)
    return _previous_period(start, end)


def _scoped_employees(db: Session, selection: ReportSelection, *, apply_status: bool = True) -> list[Employee]:
    query = select(Employee).order_by(Employee.code)
    if apply_status:
        if selection.employee_status == "active":
            query = query.where(Employee.status == "active")
        elif selection.employee_status == "archived":
            query = query.where(Employee.status == "archived")
    if selection.department_id is not None:
        query = query.where(Employee.department_id == selection.department_id)
    if selection.employee_id is not None:
        query = query.where(Employee.id == selection.employee_id)
    return list(db.scalars(query).all())


def _id_proof(employee: Employee) -> str:
    if (employee.nationality or "egyptian") == "foreign":
        return f"باسبور {employee.passport_number}" if employee.passport_number else "—"
    return employee.national_id or "—"


def _nationality_label(employee: Employee) -> str:
    return "أجنبي" if (employee.nationality or "egyptian") == "foreign" else "مصري"


def _employee_cell(employee: Employee, key: str):
    if key == "emp_name":
        return employee.full_name
    if key == "emp_code":
        return employee.code
    if key == "emp_department":
        return employee.department.name if employee.department else "—"
    if key == "emp_nationality":
        return _nationality_label(employee)
    if key == "emp_id_proof":
        return _id_proof(employee)
    if key == "emp_hire_date":
        return employee.hire_date.isoformat()
    return ""


def _payroll_items(db: Session, employee_ids: list[int], start: date, end: date, *, approved_only: bool = False) -> list[PayrollItem]:
    if not employee_ids:
        return []
    query = (
        select(PayrollItem)
        .join(PayrollRun, PayrollItem.payroll_run_id == PayrollRun.id)
        .where(PayrollRun.period_start <= end, PayrollRun.period_end >= start)
        .where(PayrollItem.employee_id.in_(employee_ids))
    )
    if approved_only:
        query = query.where(PayrollRun.status.in_(("approved", "closed")))
    return list(db.scalars(query).all())


def _item_earnings(item: PayrollItem) -> Decimal:
    return (
        item.base_salary
        + item.attendance_bonus
        + item.performance_bonus
        + item.occupancy_bonus
        + item.overtime_amount
        + item.other_bonus
        + item.staff_reward_amount
    )


def _item_deductions(item: PayrollItem, *, include_advances: bool = True) -> Decimal:
    total = (
        item.absence_deduction
        + item.lateness_deduction
        + item.insurance_share
        + item.taxes
        + item.other_deductions
        + item.staff_sanction_amount
    )
    if include_advances:
        total += item.advance_deduction
    return total


def _payroll_totals(db: Session, employee_ids: list[int], start: date, end: date, *, approved_only: bool = False) -> dict:
    items = _payroll_items(db, employee_ids, start, end, approved_only=approved_only)
    earnings = sum((_item_earnings(item) for item in items), Decimal(0))
    deductions = sum((_item_deductions(item) for item in items), Decimal(0))
    net = sum((item.net_pay for item in items), Decimal(0))
    return {
        "earnings": earnings,
        "deductions": deductions,
        "net": net,
        "employees": len({item.employee_id for item in items}),
    }


def _attendance_stats(db: Session, employee_ids: list[int], start: date, end: date, *, approved_only: bool = False) -> dict[int, dict]:
    stats = {emp_id: {"present": 0, "unreviewed": 0} for emp_id in employee_ids}
    if not employee_ids:
        return stats
    query = (
        select(AttendanceDailyRecord)
        .where(AttendanceDailyRecord.work_date >= start, AttendanceDailyRecord.work_date <= end)
        .where(AttendanceDailyRecord.employee_id.in_(employee_ids))
    )
    if approved_only:
        query = query.where(AttendanceDailyRecord.status == "approved")
    records = db.scalars(query).all()
    for record in records:
        bucket = stats.setdefault(record.employee_id, {"present": 0, "unreviewed": 0})
        if record.status == "approved" and (record.worked_minutes or 0) > 0:
            bucket["present"] += 1
        if record.status in ("needs_review", "ready_for_review"):
            bucket["unreviewed"] += 1
    return stats


def _sum_items(items: list[PayrollItem], attribute: str) -> Decimal:
    return sum((getattr(item, attribute) for item in items), Decimal(0))


def _L(text, language: str):
    """Translate an authored Arabic string for exports; data passes through unchanged."""
    if not isinstance(text, str) or not text or language == "ar":
        return text
    from app.i18n import EN, ZH
    table = EN if language == "en" else ZH
    if text in table:
        return table[text]
    for source in sorted(table, key=len, reverse=True):
        if source.endswith(":") and text.startswith(source + " "):
            return table[source] + text[len(source):]
    if " — " in text:
        return " — ".join(_L(part, language) for part in text.split(" — "))
    out = text
    for month_ar in _AR_MONTHS:
        translated = table.get(month_ar)
        if translated and month_ar in out:
            out = out.replace(month_ar, translated)
    if out.endswith(" ج.م") and "ج.م" in table:
        out = out[:-4] + " " + table["ج.م"]
    return out


def _warning_text_ar(warning, language: str = "ar") -> str:
    if isinstance(warning, str):
        return _L(warning, language)
    if warning.get("kind") == "no_payroll":
        return f"{warning['count']} {_L('موظف بدون راتب مسجل في الفترة:', language)} {warning['details']}"
    if warning.get("kind") == "unreviewed_days":
        return f"{warning['count']} {_L('يوم حضور غير محسوم في الفترة لا يدخل في أيام العمل.', language)}"
    return f"{warning['count']} {_L('يوم حضور بانتظار المراجعة؛ أيام الغياب والتأخير تُحسب من مسيرات الرواتب المعتمدة فقط.', language)}"


@dataclass
class ReportResult:
    report_type: str
    title: str
    period_label: str
    start_date: date
    end_date: date
    scope_label: str
    status_label: str
    employee_count: int
    columns: list[tuple[str, str, bool]]
    groups: list[dict]
    totals: dict
    summary: list[tuple[str, object]]
    comparison: dict | None
    warnings: list[dict]
    generated_at: datetime | None = None
    empty: bool = False


# Per-employee amount explanation: every component that builds the net pay,
# in display order, as (key, Arabic label, is_deduction).
PAYROLL_BREAKDOWN_DEFS = [
    ("salary_basis", "أساس الاحتساب", False),
    ("base_salary", "الأساسي بعد التناسب", False),
    ("attendance_bonus", "بدل حضور", False),
    ("performance_bonus", "بدل أداء", False),
    ("occupancy_bonus", "بدل إشغال", False),
    ("overtime_amount", "إضافي", False),
    ("other_bonus", "مكافآت أخرى", False),
    ("staff_reward_amount", "مكافأة مالية", False),
    ("absence_deduction", "خصم غياب", True),
    ("lateness_deduction", "خصم تأخير", True),
    ("insurance_share", "تأمينات", True),
    ("taxes", "ضرائب", True),
    ("other_deductions", "خصومات أخرى", True),
    ("advance_deduction", "خصم السلفة", True),
    ("staff_sanction_amount", "جزاء مالي", True),
    ("net_pay", "صافي المرتب", False),
]


def _payroll_breakdown(items: list[PayrollItem]) -> list[tuple[str, Decimal, bool]]:
    """Sum every pay component across the period's runs for one employee."""
    return [(label, _sum_items(items, key), is_deduction) for key, label, is_deduction in PAYROLL_BREAKDOWN_DEFS]


def _ordered_columns(selection: ReportSelection, group_keys: list[str]) -> list[tuple[str, str, bool]]:
    columns = []
    for group_key in group_keys:
        for key, label in FIELD_GROUP_DEFS[group_key][1]:
            if key in selection.fields:
                columns.append((key, label, key in NUMERIC_FIELDS))
    return columns


def _group_rows(rows: list[dict], employees: dict[int, Employee]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in rows:
        employee = employees[row["employee_id"]]
        department = employee.department.name if employee.department else "بدون قسم"
        if department not in grouped:
            grouped[department] = []
            order.append(department)
        grouped[department].append(row)
    return [{"department": name, "rows": grouped[name]} for name in sorted(order)]


def _fmt_number(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        rounded = round(value, 2)
        if rounded == int(rounded):
            return f"{int(rounded):,}"
        return f"{rounded:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _pct_change(current: Decimal, previous: Decimal) -> str | None:
    if previous == 0:
        return None
    change = (current - previous) / previous * 100
    return f"{float(change):+.1f}%"


def _build_payroll_result(db: Session, selection: ReportSelection, start: date, end: date) -> ReportResult:
    employees = _scoped_employees(db, selection)
    employee_map = {employee.id: employee for employee in employees}
    items = _payroll_items(db, list(employee_map), start, end, approved_only=selection.approved_only)
    items_by_employee: dict[int, list[PayrollItem]] = {}
    for item in items:
        items_by_employee.setdefault(item.employee_id, []).append(item)

    attendance = _attendance_stats(db, list(employee_map), start, end, approved_only=selection.approved_only)
    columns = _ordered_columns(selection, ["employee", "payroll", "attendance"])
    rows = []
    for employee_id, employee_items in sorted(items_by_employee.items(), key=lambda pair: employee_map[pair[0]].code):
        employee = employee_map[employee_id]
        row = {"employee_id": employee_id}
        for key, _, _ in columns:
            if key.startswith("emp_"):
                row[key] = _employee_cell(employee, key)
        row["pay_basic"] = _sum_items(employee_items, "base_salary")
        row["pay_allowances"] = sum(
            (
                item.attendance_bonus + item.performance_bonus + item.occupancy_bonus
                + item.overtime_amount + item.other_bonus + item.staff_reward_amount
                for item in employee_items
            ),
            Decimal(0),
        )
        row["pay_deductions"] = sum((_item_deductions(item, include_advances=False) for item in employee_items), Decimal(0))
        row["pay_advances"] = _sum_items(employee_items, "advance_deduction")
        row["pay_net"] = _sum_items(employee_items, "net_pay")
        row["pay_work_days"] = attendance[employee_id]["present"]
        row["att_present"] = attendance[employee_id]["present"]
        row["att_absent"] = _sum_items(employee_items, "absence_days")
        row["att_late"] = _sum_items(employee_items, "late_minutes")
        row["att_overtime"] = _sum_items(employee_items, "overtime_hours")
        row["breakdown"] = _payroll_breakdown(employee_items)
        rows.append(row)

    totals = {}
    for key, _, numeric in columns:
        if numeric:
            totals[key] = sum((row[key] for row in rows), Decimal(0)) if rows else Decimal(0)

    totals_all = _payroll_totals(db, list(employee_map), start, end, approved_only=selection.approved_only)
    summary = [
        ("إجمالي المستحقات", f"{_fmt_number(totals_all['earnings'])} ج.م"),
        ("إجمالي الاستقطاعات", f"{_fmt_number(totals_all['deductions'])} ج.م"),
        ("صافي المرتبات", f"{_fmt_number(totals_all['net'])} ج.م"),
        ("عدد الموظفين", totals_all["employees"]),
    ]

    if selection.monthly_calendar_comparison:
        prev_start, prev_end = _previous_calendar_month(start, end)
    else:
        prev_start, prev_end = _previous_period(start, end)
    # The previous period must reflect who was actually paid then, not today's status:
    # include every employee in scope with payroll evidence in that period.
    history_employees = _scoped_employees(db, selection, apply_status=False)
    history_map = {employee.id: employee for employee in history_employees}
    previous = _payroll_totals(db, list(history_map), prev_start, prev_end, approved_only=selection.approved_only)
    comparison = None
    if previous["employees"] > 0:
        # Membership in each period comes from payroll evidence, not current status.
        prev_items = _payroll_items(db, list(history_map), prev_start, prev_end, approved_only=selection.approved_only)
        current_ids = set(items_by_employee)
        previous_ids = {item.employee_id for item in prev_items}
        continuing = current_ids & previous_ids
        joiners = current_ids - previous_ids
        leavers = previous_ids - current_ids
        continuing_items_current = [item for item in items if item.employee_id in continuing]
        continuing_items_previous = [item for item in prev_items if item.employee_id in continuing]
        cont_cur_net = sum((item.net_pay for item in continuing_items_current), Decimal(0))
        cont_prev_net = sum((item.net_pay for item in continuing_items_previous), Decimal(0))
        joiner_net = sum((item.net_pay for item in items if item.employee_id in joiners), Decimal(0))
        leaver_prev_net = sum((item.net_pay for item in prev_items if item.employee_id in leavers), Decimal(0))
        # A "leaver" who is still active may simply lack a payroll run yet.
        still_active_leavers = sum(1 for emp_id in leavers if history_map[emp_id].status == "active")
        comparison = {
            "period_label": f"{prev_start.strftime('%d/%m/%Y')} – {prev_end.strftime('%d/%m/%Y')}",
            "items": [
                ("المستحقات", totals_all["earnings"], previous["earnings"], _pct_change(totals_all["earnings"], previous["earnings"])),
                ("الاستقطاعات", totals_all["deductions"], previous["deductions"], _pct_change(totals_all["deductions"], previous["deductions"])),
                ("الصافي", totals_all["net"], previous["net"], _pct_change(totals_all["net"], previous["net"])),
                ("الموظفون", totals_all["employees"], previous["employees"], _pct_change(Decimal(totals_all["employees"]), Decimal(previous["employees"]))),
            ],
            "like_for_like": {
                "enabled": selection.compare_like_for_like,
                "continuing_count": len(continuing),
                "continuing_current_net": cont_cur_net,
                "continuing_previous_net": cont_prev_net,
                "continuing_pct": _pct_change(cont_cur_net, cont_prev_net),
                "joiners_count": len(joiners),
                "joiners_net": joiner_net,
                "leavers_count": len(leavers),
                "leavers_previous_net": leaver_prev_net,
                "still_active_leavers": still_active_leavers,
            },
        }

    warnings = []
    without_payroll = [employee.code for employee in employees if employee.id not in items_by_employee]
    if without_payroll and selection.employee_id is None:
        warnings.append({"kind": "no_payroll", "count": len(without_payroll), "details": "، ".join(without_payroll[:8])})
    unreviewed = sum(bucket["unreviewed"] for bucket in attendance.values())
    if unreviewed:
        warnings.append({"kind": "unreviewed_days", "count": unreviewed, "details": ""})

    return ReportResult(
        report_type=selection.report_type,
        title="تقرير الرواتب",
        period_label=month_label(start) if start.month == end.month and start.year == end.year else f"{start.strftime('%d/%m/%Y')} – {end.strftime('%d/%m/%Y')}",
        start_date=start,
        end_date=end,
        scope_label="",
        status_label="",
        employee_count=totals_all["employees"],
        columns=columns,
        groups=_group_rows(rows, employee_map),
        totals=totals,
        summary=summary,
        comparison=comparison,
        warnings=warnings,
        generated_at=datetime.now(),
        empty=not rows,
    )


def _build_attendance_result(db: Session, selection: ReportSelection, start: date, end: date) -> ReportResult:
    employees = _scoped_employees(db, selection)
    employee_map = {employee.id: employee for employee in employees}
    attendance = _attendance_stats(db, list(employee_map), start, end, approved_only=selection.approved_only)
    items = _payroll_items(db, list(employee_map), start, end, approved_only=selection.approved_only)
    items_by_employee: dict[int, list[PayrollItem]] = {}
    for item in items:
        items_by_employee.setdefault(item.employee_id, []).append(item)

    columns = _ordered_columns(selection, ["employee", "attendance"])
    rows = []
    for employee in employees:
        stats = attendance[employee.id]
        employee_items = items_by_employee.get(employee.id, [])
        if not stats["present"] and not stats["unreviewed"] and not employee_items:
            continue
        row = {"employee_id": employee.id}
        for key, _, _ in columns:
            if key.startswith("emp_"):
                row[key] = _employee_cell(employee, key)
        row["att_present"] = stats["present"]
        row["att_absent"] = _sum_items(employee_items, "absence_days") if employee_items else None
        row["att_late"] = _sum_items(employee_items, "late_minutes") if employee_items else None
        row["att_overtime"] = _sum_items(employee_items, "overtime_hours") if employee_items else None
        rows.append(row)

    totals = {}
    for key, _, numeric in columns:
        if numeric:
            totals[key] = sum((row[key] for row in rows if row[key] is not None), Decimal(0))

    summary = [
        ("أيام الحضور المعتمدة", sum(bucket["present"] for bucket in attendance.values())),
        ("أيام غير محسومة", sum(bucket["unreviewed"] for bucket in attendance.values())),
        ("عدد الموظفين", len(rows)),
    ]

    if selection.monthly_calendar_comparison:
        prev_start, prev_end = _previous_calendar_month(start, end)
    else:
        prev_start, prev_end = _previous_period(start, end)
    prev_attendance = _attendance_stats(db, list(employee_map), prev_start, prev_end, approved_only=selection.approved_only)
    prev_present = sum(bucket["present"] for bucket in prev_attendance.values())
    cur_present = sum(bucket["present"] for bucket in attendance.values())
    comparison = None
    if prev_present:
        comparison = {
            "period_label": f"{prev_start.strftime('%d/%m/%Y')} – {prev_end.strftime('%d/%m/%Y')}",
            "items": [
                ("أيام الحضور", Decimal(cur_present), Decimal(prev_present), _pct_change(Decimal(cur_present), Decimal(prev_present))),
            ],
        }

    warnings = []
    unreviewed = sum(bucket["unreviewed"] for bucket in attendance.values())
    if unreviewed:
        warnings.append({"kind": "unreviewed_attendance", "count": unreviewed, "details": ""})

    return ReportResult(
        report_type=selection.report_type,
        title="تقرير الحضور والانصراف",
        period_label=month_label(start) if start.month == end.month and start.year == end.year else f"{start.strftime('%d/%m/%Y')} – {end.strftime('%d/%m/%Y')}",
        start_date=start,
        end_date=end,
        scope_label="",
        status_label="",
        employee_count=len(rows),
        columns=columns,
        groups=_group_rows(rows, employee_map),
        totals=totals,
        summary=summary,
        comparison=comparison,
        warnings=warnings,
        generated_at=datetime.now(),
        empty=not rows,
    )


def _record_rows(employees: dict[int, Employee], records, employee_columns, record_columns) -> list[dict]:
    rows = []
    for record in records:
        employee = employees.get(record.employee_id)
        if employee is None:
            continue
        row = {"employee_id": record.employee_id}
        for key in employee_columns:
            row[key] = _employee_cell(employee, key)
        for key, getter in record_columns:
            row[key] = getter(record)
        rows.append(row)
    return rows


def _build_advances_result(db: Session, selection: ReportSelection, start: date, end: date) -> ReportResult:
    employees = _scoped_employees(db, selection)
    employee_map = {employee.id: employee for employee in employees}
    records = list(
        db.scalars(
            select(AdvanceRequest)
            .where(AdvanceRequest.request_date >= start, AdvanceRequest.request_date <= end)
            .where(AdvanceRequest.employee_id.in_(list(employee_map) or [0]))
            .order_by(AdvanceRequest.request_date, AdvanceRequest.id)
        ).all()
    )
    employee_columns = [key for key, _, _ in _ordered_columns(selection, ["employee"])]
    record_columns = [
        ("request_date", lambda r: r.request_date.strftime("%d/%m/%Y")),
        ("amount", lambda r: r.amount),
        ("advance_type", lambda r: "مقسطة" if r.advance_type == "installment" else "شهرية"),
        ("status", lambda r: ADVANCE_STATUS_LABELS.get(r.status, r.status)),
        ("payroll_month", lambda r: r.payroll_month.strftime("%m/%Y") if r.payroll_month else "—"),
    ]
    columns = [(k, FIELD_LABELS[k], False) for k in employee_columns] + [
        ("request_date", "تاريخ الطلب", False),
        ("amount", "المبلغ", True),
        ("advance_type", "النوع", False),
        ("status", "الحالة", False),
        ("payroll_month", "شهر الخصم", False),
    ]
    rows = _record_rows(employee_map, records, employee_columns, record_columns)
    total_amount = sum((row["amount"] for row in rows), Decimal(0))
    summary = [
        ("عدد الطلبات", len(rows)),
        ("إجمالي المبالغ", f"{_fmt_number(total_amount)} ج.م"),
        ("مصروفة أو مخصومة", sum(1 for r in records if r.status in ("paid", "deducted"))),
    ]
    return ReportResult(
        report_type=selection.report_type,
        title="تقرير السلف",
        period_label=month_label(start) if start.month == end.month and start.year == end.year else f"{start.strftime('%d/%m/%Y')} – {end.strftime('%d/%m/%Y')}",
        start_date=start,
        end_date=end,
        scope_label="", status_label="", employee_count=len({r.employee_id for r in records}),
        columns=columns,
        groups=_group_rows(rows, employee_map),
        totals={"amount": total_amount},
        summary=summary,
        comparison=None,
        warnings=[],
        generated_at=datetime.now(),
        empty=not rows,
    )


def _build_leave_result(db: Session, selection: ReportSelection, start: date, end: date) -> ReportResult:
    employees = _scoped_employees(db, selection)
    employee_map = {employee.id: employee for employee in employees}
    records = list(
        db.scalars(
            select(LeaveRecord)
            .where(LeaveRecord.start_date <= end, LeaveRecord.end_date >= start)
            .where(LeaveRecord.employee_id.in_(list(employee_map) or [0]))
            .order_by(LeaveRecord.start_date, LeaveRecord.id)
        ).all()
    )
    if selection.monthly_leave_status is not None:
        if selection.monthly_leave_status not in LEAVE_STATUS_LABELS:
            raise ValueError("Exact monthly leave status required")
        records = [r for r in records if r.status == selection.monthly_leave_status]
    leave_types = {item.id: (getattr(item, "name_" + selection.monthly_leave_language, None) or (_L("ترجمة نوع الإجازة غير متاحة", selection.monthly_leave_language) + " [" + str(item.id) + "]"))
                   if selection.monthly_leave_language in ("ar", "en", "zh") else item.name_ar
                   for item in db.scalars(select(LeaveType)).all()}
    employee_columns = [key for key, _, _ in _ordered_columns(selection, ["employee"])]
    if selection.monthly_leave_clipping:
        # Original start/end stay as context; the day columns reflect this
        # month only so per-employee detail matches the aggregate totals.
        clipped_days = {
            r.id: (min(r.end_date, end) - max(r.start_date, start)).days + 1
            for r in records
        }
        charged_here = {
            r.id: (r.charged_days if r.start_date >= start else 0)
            for r in records
        }
    else:
        clipped_days = {r.id: r.calendar_days for r in records}
        charged_here = {r.id: r.charged_days for r in records}
    record_columns = [
        ("leave_type", lambda r: leave_types.get(r.leave_type_id, "—")),
        ("leave_start", lambda r: r.start_date.strftime("%d/%m/%Y")),
        ("leave_end", lambda r: r.end_date.strftime("%d/%m/%Y")),
        ("calendar_days", lambda r: clipped_days[r.id]),
        ("charged_days", lambda r: charged_here[r.id]),
        ("status", lambda r: LEAVE_STATUS_LABELS.get(r.status, r.status)),
    ]
    columns = [(k, FIELD_LABELS[k], False) for k in employee_columns] + [
        ("leave_type", "نوع الإجازة", False),
        ("leave_start", "من", False),
        ("leave_end", "إلى", False),
        ("calendar_days", "الأيام", True),
        ("charged_days", "أيام الخصم المسجلة" if selection.monthly_leave_status else "الخصم من الرصيد", True),
        ("status", "الحالة", False),
    ]
    rows = _record_rows(employee_map, records, employee_columns, record_columns)
    if selection.monthly_leave_status is not None:
        columns.insert(len(employee_columns), ("employment_status", "حالة الموظف الحالية", False))
        for row in rows:
            status = employee_map[row["employee_id"]].status
            row["employment_status"] = {"active": "نشط", "archived": "غادر"}.get(status, "حالة موظف غير معروفة")
    summary = [
        ("عدد الإجازات", len(rows)),
        (LEAVE_STATUS_LABELS[selection.monthly_leave_status], len(records)) if selection.monthly_leave_status else
        ("مؤكدة", sum(1 for r in records if r.status == "confirmed")),
        ("إجمالي الأيام", sum(clipped_days[r.id] for r in records)),
    ]
    return ReportResult(
        report_type=selection.report_type,
        title="تقرير الإجازات",
        period_label=month_label(start) if start.month == end.month and start.year == end.year else f"{start.strftime('%d/%m/%Y')} – {end.strftime('%d/%m/%Y')}",
        start_date=start,
        end_date=end,
        scope_label="", status_label="", employee_count=len({r.employee_id for r in records}),
        columns=columns,
        groups=_group_rows(rows, employee_map),
        totals={"calendar_days": sum(clipped_days[r.id] for r in records), "charged_days": (_monthly_leave_charged_total(charged_here[r.id] for r in records) if selection.monthly_leave_clipping else sum(float(charged_here[r.id] or 0) for r in records))},
        summary=summary,
        comparison=None,
        warnings=[],
        generated_at=datetime.now(),
        empty=not rows,
    )


def _build_staff_actions_result(db: Session, selection: ReportSelection, start: date, end: date) -> ReportResult:
    employees = _scoped_employees(db, selection)
    employee_map = {employee.id: employee for employee in employees}
    from datetime import datetime, time as dtime

    records = list(
        db.scalars(
            select(StaffAction)
            .where(
                StaffAction.created_at >= datetime.combine(start, dtime.min, tzinfo=CAIRO_TZ),
                StaffAction.created_at < datetime.combine(end + timedelta(days=1), dtime.min, tzinfo=CAIRO_TZ),
            )
            .where(StaffAction.employee_id.in_(list(employee_map) or [0]))
            .order_by(StaffAction.created_at, StaffAction.id)
        ).all()
    )
    employee_columns = [key for key, _, _ in _ordered_columns(selection, ["employee"])]
    record_columns = [
        ("action_date", lambda r: r.created_at.strftime("%d/%m/%Y")),
        ("kind", lambda r: ACTION_KIND_LABELS.get(r.kind, r.kind)),
        ("title", lambda r: r.title),
        ("amount", lambda r: r.amount),
        ("status", lambda r: ACTION_STATUS_LABELS.get(r.status, r.status)),
    ]
    columns = [(k, FIELD_LABELS[k], False) for k in employee_columns] + [
        ("action_date", "التاريخ", False),
        ("kind", "النوع", False),
        ("title", "البيان", False),
        ("amount", "المبلغ", True),
        ("status", "الحالة", False),
    ]
    rows = _record_rows(employee_map, records, employee_columns, record_columns)
    rewards = sum((r.amount or 0 for r in records if r.kind == "reward" and r.status == "approved"), Decimal(0))
    sanctions = sum((r.amount or 0 for r in records if r.kind == "sanction" and r.status == "approved"), Decimal(0))
    summary = [
        ("عدد الإجراءات", len(rows)),
        ("مكافآت مالية معتمدة", f"{_fmt_number(rewards)} ج.م"),
        ("جزاءات مالية معتمدة", f"{_fmt_number(sanctions)} ج.م"),
    ]
    return ReportResult(
        report_type=selection.report_type,
        title="تقرير الجزاءات والمكافآت",
        period_label=month_label(start) if start.month == end.month and start.year == end.year else f"{start.strftime('%d/%m/%Y')} – {end.strftime('%d/%m/%Y')}",
        start_date=start,
        end_date=end,
        scope_label="", status_label="", employee_count=len({r.employee_id for r in records}),
        columns=columns,
        groups=_group_rows(rows, employee_map),
        totals={"amount": sum((row["amount"] or 0 for row in rows), Decimal(0))},
        summary=summary,
        comparison=None,
        warnings=[],
        generated_at=datetime.now(),
        empty=not rows,
    )


_BUILDERS = {
    REPORT_TYPE_PAYROLL: _build_payroll_result,
    REPORT_TYPE_ATTENDANCE: _build_attendance_result,
    REPORT_TYPE_ADVANCES: _build_advances_result,
    REPORT_TYPE_LEAVE: _build_leave_result,
    REPORT_TYPE_STAFF_ACTIONS: _build_staff_actions_result,
}


def build_report(db: Session, selection: ReportSelection, today: date | None = None) -> ReportResult:
    if selection.report_type == REPORT_TYPE_COMPREHENSIVE:
        raise ValueError("التقرير الشامل يُصدَّر مباشرة دون معاينة مفصلة.")
    start, end = resolve_period(selection, today)
    result = _BUILDERS[selection.report_type](db, selection, start, end)
    # Fill scope labels now that departments/employees are known.
    departments = {d.id: d.name for d in db.scalars(select(Department)).all()}
    if selection.employee_id is not None:
        employee = db.get(Employee, selection.employee_id)
        result.scope_label = employee.full_name if employee else "موظف محدد"
    elif selection.department_id is not None:
        result.scope_label = departments.get(selection.department_id, "قسم محدد")
    else:
        result.scope_label = "كل الأقسام"
    result.status_label = {"active": "النشطون", "all": "الكل", "archived": "المؤرشفون"}[selection.employee_status]
    return result


def report_paper_content(result: ReportResult) -> dict:
    """One scoped content policy for normal print and custom PDF, no DB effects."""
    comparison = result.comparison
    return {
        "comparison_items": comparison["items"] if comparison else [],
        "comparison_period": comparison["period_label"] if comparison else "",
        "like_for_like": (comparison.get("like_for_like")
                          if comparison and (comparison.get("like_for_like") or {}).get("enabled") else None),
        "components_by_employee": {row["employee_id"]: row["breakdown"] for group in result.groups for row in group["rows"] if row.get("breakdown")},
        "signatures": ("إعداد", "مراجعة"),
    }


def build_custom_workbook(db: Session, selection: ReportSelection, language: str = "ar") -> bytes:
    """Render the selected report as an Excel workbook mirroring the on-screen preview."""
    result = build_report(db, selection)

    def L(text):
        return _L(text, language)

    rtl = language == "ar"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = L("التقرير")
    sheet.sheet_view.rightToLeft = rtl

    headers = [L(label) for _, label, _ in result.columns]
    title_font = Font(bold=True, size=14)
    sheet.append([f"{L(result.title)} — {L(result.period_label)}"])
    sheet["A1"].font = title_font
    sheet.append([f"{L('الفترة:')} {result.start_date.strftime('%d/%m/%Y')} {L('إلى')} {result.end_date.strftime('%d/%m/%Y')} · {L(result.scope_label)} · {L(result.status_label)}"])
    sheet.append([])
    sheet.append(headers)
    header_row = sheet.max_row
    for cell in sheet[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="24415E")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for group in result.groups:
        dept_cell = [group["department"]] + [""] * (len(headers) - 1)
        sheet.append(dept_cell)
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="FAF3E3")
        for row in group["rows"]:
            sheet.append([
                float(row[key]) if isinstance(row.get(key), Decimal) else L(row.get(key, ""))
                for key, _, _ in result.columns
            ])
    if result.totals:
        totals_row = []
        for key, _, numeric in result.columns:
            if numeric and key in result.totals:
                value = result.totals[key]
                totals_row.append(float(value) if isinstance(value, Decimal) else value)
            elif not any(totals_row):
                totals_row.append(L("الإجمالي"))
            else:
                totals_row.append("")
        sheet.append(totals_row)
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="FDF6E4")

    for column_cells in sheet.columns:
        max_length = max((len(str(cell.value or "")) for cell in column_cells), default=10)
        sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max(max_length + 4, 12), 36)

    summary_sheet = workbook.create_sheet(L("الملخص"))
    summary_sheet.sheet_view.rightToLeft = rtl
    summary_sheet.append([L("البند"), L("القيمة")])
    for label, value in result.summary:
        summary_sheet.append([L(label), L(str(value))])
    for cell in summary_sheet[1]:
        cell.font = Font(bold=True)
    summary_sheet.column_dimensions["A"].width = 28
    summary_sheet.column_dimensions["B"].width = 24

    if result.comparison:
        cmp_sheet = workbook.create_sheet(L("المقارنة"))
        cmp_sheet.sheet_view.rightToLeft = rtl
        cmp_sheet.append([L("البند"), L("الفترة الحالية"), f"{L('الفترة السابقة')} ({result.comparison['period_label']})", L("التغير")])
        for label, current, previous, pct in result.comparison["items"]:
            cmp_sheet.append([
                L(label),
                float(current) if isinstance(current, Decimal) else current,
                float(previous) if isinstance(previous, Decimal) else previous,
                pct or "—",
            ])
        lfl = result.comparison.get("like_for_like")
        if lfl and lfl.get("enabled"):
            cmp_sheet.append([])
            cmp_sheet.append([L("مقارنة مثل بمثل (المستمرون في الفترتين)")])
            cmp_sheet[cmp_sheet.max_row][0].font = Font(bold=True)
            cmp_sheet.append([L("عدد المستمرين"), lfl["continuing_count"]])
            cmp_sheet.append([L("صافي المستمرين — حالية"), float(lfl["continuing_current_net"])])
            cmp_sheet.append([L("صافي المستمرين — سابقة"), float(lfl["continuing_previous_net"])])
            cmp_sheet.append([L("التغير مثل بمثل"), lfl["continuing_pct"] or "—"])
            cmp_sheet.append([L("منضمون في الفترة الحالية (عدد / صافي)"), f"{lfl['joiners_count']} / {_fmt_number(lfl['joiners_net'])}"])
            cmp_sheet.append([L("غير موجودين في الفترة الحالية (عدد / صافيهم السابق)"), f"{lfl['leavers_count']} / {_fmt_number(lfl['leavers_previous_net'])}"])
            if lfl["still_active_leavers"]:
                cmp_sheet.append([L("تنبيه"), f"{lfl['still_active_leavers']} {L('منهم ما زالوا نشطين؛ قد يعني غيابهم عدم إصدار مسير الفترة بعد.')}"])
        for cell in cmp_sheet[1]:
            cell.font = Font(bold=True)
        for letter in ("A", "B", "C", "D"):
            cmp_sheet.column_dimensions[letter].width = 34

    breakdown_rows = [
        (group["department"], row)
        for group in result.groups
        for row in group["rows"]
        if row.get("breakdown")
    ]
    if breakdown_rows:
        detail_sheet = workbook.create_sheet(L("تفصيل الحساب"))
        detail_sheet.sheet_view.rightToLeft = rtl
        detail_sheet.append([L("القسم"), L("رقم الموظف"), L("اسم الموظف")] + [L(label) for _, label, _ in PAYROLL_BREAKDOWN_DEFS])
        for cell in detail_sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="24415E")
        for department, row in breakdown_rows:
            detail_sheet.append(
                [department, row.get("emp_code", ""), row.get("emp_name", "")]
                + [float(value) for _, value, _ in row["breakdown"]]
            )
        detail_sheet.freeze_panes = "D2"
        for column_cells in detail_sheet.columns:
            max_length = max((len(str(cell.value or "")) for cell in column_cells), default=10)
            detail_sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max(max_length + 4, 12), 30)

    if result.warnings:
        warn_sheet = workbook.create_sheet(L("تنبيهات"))
        warn_sheet.sheet_view.rightToLeft = rtl
        for warning in result.warnings:
            warn_sheet.append([_warning_text_ar(warning, language)])
        warn_sheet.column_dimensions["A"].width = 80

    meta_sheet = workbook.create_sheet(L("بيانات الاستخراج"))
    meta_sheet.sheet_view.rightToLeft = rtl
    generated = result.generated_at.strftime('%d/%m/%Y %H:%M') if result.generated_at else ""
    meta_sheet.append([L("وقت استخراج التقرير"), generated])
    meta_sheet.append([L("إعداد"), ""])
    meta_sheet.append([L("مراجعة"), ""])
    meta_sheet.column_dimensions["A"].width = 28
    meta_sheet.column_dimensions["B"].width = 30

    neutralize_formula_cells(workbook)  # typed names/departments/reasons are text, never formulas
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


class _NumberedCanvas:
    """Canvas wrapper adding «page X of Y», extraction time, and hotel name to every page."""

    @staticmethod
    def build(font_name: str, footer_text: str, language: str = "ar", cell_renderer=None):
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas as pdf_canvas

        class NumberedCanvas(pdf_canvas.Canvas):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._saved_pages = []

            def showPage(self):
                self._saved_pages.append(dict(self.__dict__))
                self._startPage()

            def save(self):
                total = len(self._saved_pages)
                for state in self._saved_pages:
                    self.__dict__.update(state)
                    self.setFont(font_name, 7)
                    self.setFillGray(0.35)
                    from app.reports import _pdf_cell as cell

                    if language == "en":
                        page_text = f"Page {self._pageNumber} of {total}"
                    elif language == "zh":
                        page_text = f"第 {self._pageNumber} 页，共 {total} 页"
                    else:
                        page_text = f"صفحة {self._pageNumber} من {total}"
                    if cell_renderer is None:
                        self.drawRightString(self._pagesize[0] - 10 * mm, 7 * mm, cell(page_text))
                        self.drawString(10 * mm, 7 * mm, cell(footer_text))
                    else:
                        from reportlab.platypus import Paragraph
                        from reportlab.lib.styles import ParagraphStyle
                        for text, x in [(footer_text, 10 * mm), (page_text, self._pagesize[0] - 55 * mm)]:
                            para = Paragraph(cell_renderer(text), ParagraphStyle("monthlyFooter", fontName=font_name, fontSize=7, leading=9))
                            para.wrap(180 * mm, 10 * mm)
                            para.drawOn(self, x, 7 * mm)
                    super().showPage()
                super().save()

        return NumberedCanvas


def build_custom_pdf(db: Session, selection: ReportSelection, language: str = "ar") -> bytes:
    """Render the selected report as a printable PDF matching the preview."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A3, A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    from app.reports import _pdf_cell, _pdf_font_name

    result = build_report(db, selection)
    paper = report_paper_content(result)

    def L(text):
        return _L(text, language)

    if language == "zh":
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        font_name = "STSong-Light"
    else:
        font_name = _pdf_font_name()
    if language == "zh":
        from app.reports import _pdf_cell_mixed
        arabic_font = _pdf_font_name()

        def cell_(value):
            return _pdf_cell_mixed(value, arabic_font)
    else:
        cell_ = _pdf_cell
    base_styles = getSampleStyleSheet()
    title_style = ParagraphStyle("RPTitle", parent=base_styles["Title"], fontName=font_name, fontSize=15, leading=19, alignment=TA_RIGHT, spaceAfter=3 * mm)
    meta_style = ParagraphStyle("RPMeta", parent=base_styles["BodyText"], fontName=font_name, fontSize=9, leading=12, alignment=TA_RIGHT, keepWithNext=True)
    cell_style = ParagraphStyle("RPCell", parent=base_styles["BodyText"], fontName=font_name, fontSize=7.5, leading=9.5, alignment=TA_RIGHT, wordWrap="CJK")
    header_style = ParagraphStyle("RPHeader", parent=cell_style, fontSize=8, leading=10, alignment=TA_CENTER, textColor=colors.white)

    column_count = max(1, len(result.columns))
    page_size = landscape(A4) if column_count <= 7 else landscape(A3)
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=page_size,
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"{L(result.title)} {L(result.period_label)}",
        author="M.Y. Business Hotel HR System",
    )
    story = [
        Paragraph(cell_(f"{L(result.title)} — {L(result.period_label)}"), title_style),
        Paragraph(cell_(f"{L('الفترة:')} {result.start_date.strftime('%d/%m/%Y')} {L('إلى')} {result.end_date.strftime('%d/%m/%Y')} · {L(result.scope_label)} · {L(result.status_label)} · {result.employee_count} {L('موظف')}"), meta_style),
        Spacer(1, 3 * mm),
    ]

    breakdown_spans = []
    table_rows = [[Paragraph(cell_(L(label)), header_style) for _, label, _ in result.columns]]
    for group in result.groups:
        dept_row = [Paragraph(cell_(group["department"]), cell_style)] + [Paragraph("", cell_style)] * (column_count - 1)
        table_rows.append(dept_row)
        for row in group["rows"]:
            table_rows.append([
                Paragraph(cell_(_fmt_number(row.get(key)) if numeric else L(row.get(key, ""))), cell_style)
                for key, _, numeric in result.columns
            ])
            if row.get("breakdown"):
                components = paper["components_by_employee"][row["employee_id"]]
                half = (len(components) + 1) // 2
                component_rows = []
                for index in range(half):
                    cells = []
                    for offset in (0, half):
                        if index + offset < len(components):
                            label, value, _ = components[index + offset]
                            cells.extend([Paragraph(cell_(L(label)), cell_style), Paragraph(cell_(_fmt_number(value)), cell_style)])
                        else:
                            cells.extend([Paragraph("", cell_style), Paragraph("", cell_style)])
                    component_rows.append(cells)
                width = page_size[0] - document.leftMargin - document.rightMargin - 8
                component_table = LongTable(component_rows, colWidths=[width*.32,width*.18,width*.32,width*.18])
                component_table.setStyle(TableStyle([
                    ("GRID", (0,0), (-1,-1), .2, colors.HexColor("#AAB4BE")),
                    ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
                    ("TOPPADDING", (0,0), (-1,-1), 3), ("BOTTOMPADDING", (0,0), (-1,-1), 3),
                ]))
                breakdown_spans.append(len(table_rows))
                table_rows.append([component_table] + [""] * (column_count - 1))
    if result.totals:
        totals_cells = []
        first = True
        for key, _, numeric in result.columns:
            if numeric and key in result.totals:
                totals_cells.append(Paragraph(cell_(_fmt_number(result.totals[key])), cell_style))
            elif first:
                totals_cells.append(Paragraph(cell_(L("الإجمالي")), cell_style))
                first = False
            else:
                totals_cells.append(Paragraph("", cell_style))
        table_rows.append(totals_cells)

    available_width = page_size[0] - document.leftMargin - document.rightMargin - 8
    table = LongTable(table_rows, colWidths=[available_width / column_count] * column_count, repeatRows=1, hAlign="RIGHT")
    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24415E")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ])
    for index in breakdown_spans:
        style.add("SPAN", (0,index), (-1,index))
        style.add("NOSPLIT", (0,index-1), (-1,index))
    table.setStyle(style)
    story.append(table)

    if result.summary:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(cell_(L("الملخص")), meta_style))
        summary_rows = [[Paragraph(cell_(L(label)), cell_style), Paragraph(cell_(L(str(value))), cell_style)] for label, value in result.summary]
        summary_table = LongTable(summary_rows, colWidths=[available_width * 0.3, available_width * 0.2], hAlign="RIGHT")
        summary_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#FAF3E3")),
        ]))
        story.append(summary_table)

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(cell_(L("مقارنة بالفترة السابقة")), meta_style))
    if paper["comparison_items"]:
        story.append(Paragraph(cell_(paper["comparison_period"]), meta_style))
        comparison_rows = [[Paragraph(cell_(L(label)), header_style) for label in ("البند", "الفترة الحالية", "الفترة السابقة", "التغير")]]
        comparison_rows += [[Paragraph(cell_(L(label)), cell_style),
                            Paragraph(cell_(_fmt_number(current)), cell_style),
                            Paragraph(cell_(_fmt_number(previous)), cell_style),
                            Paragraph(cell_(pct or "—"), cell_style)]
                           for label, current, previous, pct in paper["comparison_items"]]
        comparison_table = LongTable(comparison_rows, colWidths=[available_width*.28, available_width*.24, available_width*.24, available_width*.24], repeatRows=1)
        comparison_table.setStyle(TableStyle([("GRID", (0,0), (-1,-1), .25, colors.HexColor("#AAB4BE")), ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#24415E"))]))
        story.append(comparison_table)
    else:
        story.append(Paragraph(cell_(L("لا توجد بيانات كافية للمقارنة في الفترة السابقة.")), meta_style))
    if paper["like_for_like"]:
        lfl = paper["like_for_like"]
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(cell_(f"{L('مقارنة مثل بمثل مع الفترة السابقة')} ({paper['comparison_period']})"), meta_style))
        lfl_rows = [
            [Paragraph(cell_(L("المستمرون في الفترتين")), cell_style), Paragraph(cell_(str(lfl["continuing_count"])), cell_style)],
            [Paragraph(cell_(L("صافي المستمرين — الفترة الحالية")), cell_style), Paragraph(cell_(_fmt_number(lfl["continuing_current_net"])), cell_style)],
            [Paragraph(cell_(L("صافي المستمرين — الفترة السابقة")), cell_style), Paragraph(cell_(_fmt_number(lfl["continuing_previous_net"])), cell_style)],
            [Paragraph(cell_(L("التغير مثل بمثل")), cell_style), Paragraph(cell_(lfl["continuing_pct"] or "—"), cell_style)],
            [Paragraph(cell_(L("منضمون في الفترة الحالية")), cell_style), Paragraph(cell_(f"{lfl['joiners_count']} · {L('صافي')} {_fmt_number(lfl['joiners_net'])}"), cell_style)],
            [Paragraph(cell_(L("غير موجودين في الفترة الحالية")), cell_style), Paragraph(cell_(f"{lfl['leavers_count']} · {L('صافيهم السابق')} {_fmt_number(lfl['leavers_previous_net'])}"), cell_style)],
        ]
        lfl_table = LongTable(lfl_rows, colWidths=[available_width * 0.3, available_width * 0.25], hAlign="RIGHT")
        lfl_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#FAF3E3")),
        ]))
        story.append(lfl_table)
        if lfl["still_active_leavers"]:
            story.append(Paragraph(cell_(f"{L('تنبيه:')} {lfl['still_active_leavers']} {L('ممن لا يظهرون في الفترة الحالية ما زالوا نشطين؛ قد يعني ذلك عدم إصدار مسير الفترة بعد وليس مغادرتهم.')}"), meta_style))

    if result.warnings:
        story.append(Spacer(1, 3 * mm))
        for warning in result.warnings:
            story.append(Paragraph(cell_(f"{L('تنبيه:')} {_warning_text_ar(warning, language)}"), meta_style))

    story.append(Spacer(1, 10 * mm))
    signature_rows = [[Paragraph(cell_(L(label + ": ..............................")), meta_style) for label in paper["signatures"]]]
    signature_table = LongTable(signature_rows, colWidths=[available_width / len(paper["signatures"])] * len(paper["signatures"]), hAlign="RIGHT")
    signature_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    story.append(signature_table)

    generated = result.generated_at.strftime("%d/%m/%Y %H:%M") if result.generated_at else ""
    footer_text = f"M.Y. Business Hotel — {L('استُخرج في:')} {generated}"
    document.build(story, canvasmaker=_NumberedCanvas.build(font_name, footer_text, language))
    return output.getvalue()


def list_presets(db: Session) -> list[ReportPreset]:
    return list(db.scalars(select(ReportPreset).order_by(ReportPreset.name)).all())


def save_preset(db: Session, name: str, config: dict, created_by: str, existing_check=None) -> ReportPreset:
    """Create or replace a preset by name. existing_check(stored_config) must raise
    if the caller may not replace the stored config; it runs on the row locked
    (SELECT ... FOR UPDATE) in this same transaction, so the stored config cannot
    change between the check and the write."""
    name = name.strip()
    if not name:
        raise ValueError("اسم الاختيار مطلوب.")
    if len(name) > 120:
        raise ValueError("اسم الاختيار طويل جدًا.")
    payload = json.dumps(config, ensure_ascii=False)
    for _ in range(2):
        existing = db.scalar(
            select(ReportPreset).where(ReportPreset.name == name)
            .with_for_update().execution_options(populate_existing=True)
        )
        if existing is not None:
            if existing_check is not None:
                existing_check(preset_config(existing))
            existing.config = payload
            existing.created_by = created_by
            return existing
        try:
            with db.begin_nested():
                preset = ReportPreset(name=name, config=payload, created_by=created_by)
                db.add(preset)
                db.flush()
            return preset
        except IntegrityError:
            continue  # lost the create race: re-read the winner and re-check
    raise ValueError("تعذر حفظ الاختيار الآن، حاول مرة أخرى.")


def delete_preset(db: Session, preset_id: int, existing_check=None) -> bool:
    preset = db.scalar(
        select(ReportPreset).where(ReportPreset.id == preset_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if preset is None:
        return False
    if existing_check is not None:
        existing_check(preset_config(preset))
    db.delete(preset)
    return True


def load_preset(db: Session, preset_id: int) -> ReportPreset | None:
    return db.get(ReportPreset, preset_id)


def preset_config(preset: ReportPreset) -> dict:
    try:
        config = json.loads(preset.config or "{}")
    except (TypeError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


# Monthly report (approved UI batch): a ready monthly PDF with 11 selectable
# options. Options 1-7 are content sections, 8-9 six-month charts, and 10-11
# modifiers (previous-month comparison, department grouping). The existing
# custom report flow is untouched; this builder composes the same build_report
# engine into one document.
MONTHLY_OPTION_LABELS = {
    1: "ملخص الرواتب",
    2: "تفاصيل الرواتب",
    3: "ملخص الحضور",
    4: "تفاصيل الحضور",
    5: "السلف",
    6: "الجزاءات والمكافآت",
    7: "الإجازات",
    8: "رسم إجمالي صافي الرواتب — آخر 6 أشهر",
    9: "رسم الحضور والغياب — آخر 6 أشهر",
    10: "مقارنة بالشهر السابق",
    11: "تجميع حسب القسم",
}
MONTHLY_ALL_OPTIONS = set(MONTHLY_OPTION_LABELS)
# Content option -> report type, for role scoping (charts inherit their type).
MONTHLY_OPTION_TYPE = {
    1: REPORT_TYPE_PAYROLL,
    2: REPORT_TYPE_PAYROLL,
    3: REPORT_TYPE_ATTENDANCE,
    4: REPORT_TYPE_ATTENDANCE,
    5: REPORT_TYPE_ADVANCES,
    6: REPORT_TYPE_STAFF_ACTIONS,
    7: REPORT_TYPE_LEAVE,
    8: REPORT_TYPE_PAYROLL,
    9: REPORT_TYPE_ATTENDANCE,
}
_MONTHLY_PAYROLL_SUMMARY_FIELDS = {"emp_name", "emp_code", "pay_net"}
_MONTHLY_PAYROLL_DETAIL_FIELDS = {
    "emp_name", "emp_code", "emp_department", "pay_basic", "pay_allowances",
    "pay_deductions", "pay_advances", "pay_net", "pay_work_days",
}
_MONTHLY_ATTENDANCE_SUMMARY_FIELDS = {"emp_name", "emp_code", "att_present", "att_absent"}
_MONTHLY_ATTENDANCE_DETAIL_FIELDS = {
    "emp_name", "emp_code", "emp_department",
    "att_present", "att_absent", "att_late", "att_overtime",
}
_MONTHLY_SECTION_FIELDS = {
    1: _MONTHLY_PAYROLL_SUMMARY_FIELDS,
    2: _MONTHLY_PAYROLL_DETAIL_FIELDS,
    3: _MONTHLY_ATTENDANCE_SUMMARY_FIELDS,
    4: _MONTHLY_ATTENDANCE_DETAIL_FIELDS,
}


def monthly_options_for_types(allowed_types: set[str] | None) -> set[int]:
    """Options whose underlying report type is allowed; 10/11 are modifiers."""
    data_options = {
        option for option, report_type in MONTHLY_OPTION_TYPE.items()
        if allowed_types is None or report_type in allowed_types
    }
    if not data_options:
        return set()
    return data_options | {10, 11}


def _monthly_six_month_series(db: Session, month_start: date, report_type: str) -> list[tuple[str, float | None]]:
    """Six months ending at month_start. Payroll-derived series (net pay and
    absence days) return None for a month with no approved/closed run: a
    missing run is no-data, not a truthful zero. The attendance series has its
    own payroll-independent source (approved daily records) and always renders."""
    series: list[tuple[str, float | None]] = []
    for offset in range(-5, 1):
        start = _add_months(month_start, offset)
        end = _add_months(month_start, offset + 1)
        if report_type == REPORT_TYPE_PAYROLL:
            has_run = db.scalar(
                select(func.count(PayrollRun.id)).where(
                    PayrollRun.status.in_(["approved", "closed"]),
                    PayrollRun.period_start >= start,
                    PayrollRun.period_start < end,
                )
            ) or 0
            if not has_run:
                series.append((start.strftime("%m/%Y"), None))
                continue
            value = db.scalar(
                select(func.coalesce(func.sum(PayrollItem.net_pay), 0))
                .select_from(PayrollItem)
                .join(PayrollRun, PayrollRun.id == PayrollItem.payroll_run_id)
                .where(
                    PayrollRun.status.in_(["approved", "closed"]),
                    PayrollRun.period_start >= start,
                    PayrollRun.period_start < end,
                )
            ) or 0
            series.append((start.strftime("%m/%Y"), float(value)))
        elif report_type == "absence":
            has_run = db.scalar(
                select(func.count(PayrollRun.id)).where(
                    PayrollRun.status.in_(["approved", "closed"]),
                    PayrollRun.period_start >= start,
                    PayrollRun.period_start < end,
                )
            ) or 0
            if not has_run:
                series.append((start.strftime("%m/%Y"), None))
                continue
            value = db.scalar(
                select(func.coalesce(func.sum(PayrollItem.absence_days), 0))
                .select_from(PayrollItem)
                .join(PayrollRun, PayrollRun.id == PayrollItem.payroll_run_id)
                .where(
                    PayrollRun.status.in_(["approved", "closed"]),
                    PayrollRun.period_start >= start,
                    PayrollRun.period_start < end,
                )
            ) or 0
            series.append((start.strftime("%m/%Y"), float(value)))
        else:
            value = db.scalar(
                select(func.count(AttendanceDailyRecord.id)).where(
                    AttendanceDailyRecord.status == "approved",
                    AttendanceDailyRecord.worked_minutes > 0,
                    AttendanceDailyRecord.work_date >= start,
                    AttendanceDailyRecord.work_date < end,
                )
            ) or 0
            series.append((start.strftime("%m/%Y"), float(value)))
    return series


def _monthly_chart_drawing(series: list[tuple[str, float]], bar_color_hex: str):
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.lib import colors as _colors

    width, height, chart_h, base_y = 520, 170, 120, 30
    drawing = Drawing(width, height)
    maximum = max((value for _, value in series if value is not None), default=0) or 1
    slot = width / max(1, len(series))
    bar_w = min(48, slot * 0.6)
    for index, (label, value) in enumerate(series):
        x = width - (index + 1) * slot + (slot - bar_w) / 2  # RTL: newest left
        drawing.add(String(x + bar_w / 2, base_y - 12, label, fontName="Helvetica", fontSize=8, textAnchor="middle"))
        if value is None:  # no approved run this month: a gap, not a zero
            continue
        bar_h = (value / maximum) * chart_h if value > 0 else 0.0
        drawing.add(Rect(x, base_y, bar_w, bar_h, fillColor=_colors.HexColor(bar_color_hex), strokeColor=None))
        drawing.add(String(x + bar_w / 2, base_y + bar_h + 4, ("%0.0f" % value), fontName="Helvetica", fontSize=7, textAnchor="middle"))
    return drawing


def _monthly_leave_charged_total(values) -> Decimal:
    """Exact report-only sum within the existing leave arithmetic resource envelope.

    No float conversion or inherited Decimal context may change stored charge days.
    Unsupported spans refuse rather than silently round. Custom totals are unchanged.
    """
    from app.leave_balance_rules import exact_day_difference

    total = Decimal(0)
    for value in values:
        total = exact_day_difference(total, Decimal(value or 0).copy_negate())
    return total


def _monthly_leave_aggregate(db: Session, start: date, end: date, *, all_statuses: bool = False, language: str = "ar") -> list[dict]:
    """Leave totals per type, optionally split by status, without employee identity.

    The assistant scope covers leave/balances visibility only; per-employee
    leave records stay with the roles that already have the custom leave report.
    """
    from .models import LeaveRecord as _LeaveRecord, LeaveType as _LeaveType

    records = list(
        db.scalars(
            select(_LeaveRecord)
            .where(
                *([] if all_statuses else [_LeaveRecord.status == "confirmed"]),
                _LeaveRecord.start_date <= end,
                _LeaveRecord.end_date >= start,
            )
        ).all()
    )
    type_names = {item.id: (getattr(item, "name_" + language, None) or (_L("ترجمة نوع الإجازة غير متاحة", language) + " [" + str(item.id) + "]")) for item in db.scalars(select(_LeaveType)).all()}
    buckets: dict[int | tuple[int, str], dict] = {}
    for record in records:
        bucket = buckets.setdefault(
            (record.leave_type_id, record.status) if all_statuses else record.leave_type_id,
            {"leave_type": type_names.get(record.leave_type_id, "—"), "count": 0, "days": Decimal(0), "charged": Decimal(0),
             **({"status": record.status} if all_statuses else {})},
        )
        bucket["count"] += 1
        # Owner decision 2026-11-23: a leave spanning months shows each month's
        # own calendar days in that month's report; the admin-entered charged
        # days count once, in the month the leave starts. Report-only.
        overlap_days = (min(record.end_date, end) - max(record.start_date, start)).days + 1
        bucket["days"] += Decimal(overlap_days)
        if record.start_date >= start:
            bucket["charged"] = (_monthly_leave_charged_total((bucket["charged"], record.charged_days)) if all_statuses else bucket["charged"] + Decimal(record.charged_days or 0))
    return sorted(buckets.values(), key=lambda row: (row["leave_type"], row.get("status", "")))


def _payroll_month_summary(db: Session, start: date, end: date) -> dict:
    """Totals-only payroll summary for one month (summary option, not detail).

    Source matches the monthly charts: approved/closed runs only. The custom
    report engine keeps its own semantics; this helper is wrapper-only.
    """
    from sqlalchemy import select as _select
    from .models import Employee as _Employee, PayrollRun as _PayrollRun

    employee_ids = list(db.scalars(_select(_Employee.id)).all())

    def items_for(period_start: date, period_end: date):
        return list(
            db.scalars(
                _select(PayrollItem)
                .join(_PayrollRun, PayrollItem.payroll_run_id == _PayrollRun.id)
                .where(
                    _PayrollRun.status.in_(["approved", "closed"]),
                    _PayrollRun.period_start <= period_end,
                    _PayrollRun.period_end >= period_start,
                )
                .where(PayrollItem.employee_id.in_(employee_ids))
            ).all()
        )

    def totals(items) -> dict:
        base = sum((i.base_salary for i in items), Decimal(0))
        allowances = sum((_item_earnings(i) - i.base_salary for i in items), Decimal(0))
        deductions = sum((_item_deductions(i, include_advances=False) for i in items), Decimal(0))
        advances = sum((i.advance_deduction for i in items), Decimal(0))
        net = sum((i.net_pay for i in items), Decimal(0))
        return {
            "base": base,
            "allowances": allowances,
            "deductions": deductions,
            "advances": advances,
            "net": net,
            "employees": len({i.employee_id for i in items}),
        }

    def by_department(items) -> list[dict]:
        buckets: dict[str, dict] = {}
        for item in items:
            bucket = buckets.setdefault(
                item.department_name_snapshot,
                {"department": item.department_name_snapshot, "net": Decimal(0), "employees": set()},
            )
            bucket["net"] += item.net_pay
            bucket["employees"].add(item.employee_id)
        return [
            {"department": b["department"], "net": b["net"], "employees": len(b["employees"])}
            for b in sorted(buckets.values(), key=lambda row: row["department"])
        ]

    current_items = items_for(start, end)
    prev_start, prev_end = _previous_calendar_month(start, end)
    previous_items = items_for(prev_start, prev_end)
    return {
        "current": totals(current_items),
        "previous": totals(previous_items),
        "current_approved_runs": db.scalar(select(func.count(PayrollRun.id)).where(
            PayrollRun.status.in_(("approved", "closed")),
            PayrollRun.period_start <= end, PayrollRun.period_end >= start)) or 0,
        "current_items": len(current_items),
        "period_label": f"{prev_start.strftime('%d/%m/%Y')} – {prev_end.strftime('%d/%m/%Y')}",
        "by_department": by_department(current_items),
    }


def _monthly_chart_drawing_grouped(
    series_a: list[tuple[str, float | None]],
    series_b: list[tuple[str, float | None]],
    color_a_hex: str,
    color_b_hex: str,
    legend_a: str,
    legend_b: str,
):
    """Two series per month (attendance vs absence), with legend and values."""
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.lib import colors as _colors

    width, height, chart_h, base_y = 520, 170, 120, 30
    drawing = Drawing(width, height)
    maximum = max([v for _, v in series_a if v is not None] + [v for _, v in series_b if v is not None] + [0]) or 1
    slot = width / max(1, len(series_a))
    bar_w = min(22, slot * 0.28)
    for index, (label, value) in enumerate(series_a):
        center = width - (index + 1) * slot + slot / 2  # RTL: newest left
        x_a, x_b = center - bar_w - 1.5, center + 1.5
        h_a = (value / maximum) * chart_h if value else 0.0
        v_b = series_b[index][1]
        h_b = (v_b / maximum) * chart_h if v_b else 0.0
        if value is not None:  # attendance always renders from its own source
            drawing.add(Rect(x_a, base_y, bar_w, h_a, fillColor=_colors.HexColor(color_a_hex), strokeColor=None))
        if v_b is not None:  # absence: gap when the month has no approved run
            drawing.add(Rect(x_b, base_y, bar_w, h_b, fillColor=_colors.HexColor(color_b_hex), strokeColor=None))
        if value is not None:
            drawing.add(String(x_a + bar_w / 2, base_y + h_a + 3, ("%0.0f" % value), fontName="Helvetica", fontSize=6.5, textAnchor="middle"))
        if v_b is not None:
            drawing.add(String(x_b + bar_w / 2, base_y + h_b + 3, ("%0.0f" % v_b), fontName="Helvetica", fontSize=6.5, textAnchor="middle"))
        drawing.add(String(center, base_y - 12, label, fontName="Helvetica", fontSize=8, textAnchor="middle"))
    return drawing


def _flatten_groups(groups: list[dict]) -> list[dict]:
    """Grouping modifier off: merge department groups into one 'الكل' table."""
    merged_rows = [row for group in groups for row in group["rows"]]
    return [{"department": "الكل", "rows": merged_rows}] if merged_rows else []


def monthly_date_bounds(month_start: date, options: set[int]) -> tuple[date, date]:
    """Validate only the calendar arithmetic used by the selected monthly paths.

    No arbitrary business-date cutoff. A selected comparison/trend must have
    representable predecessor months and every report needs an exclusive end.
    """
    start = month_start.replace(day=1)
    try:
        end = _add_months(start, 1) - timedelta(days=1)
        if set(options) & {8, 9}:
            _add_months(start, -5)
        elif set(options) & {1, 2, 3, 4}:
            _add_months(start, -1)
    except (ValueError, OverflowError):
        raise ValueError("اختر شهرًا صالحًا للتقرير الشهري.") from None
    return start, end


def build_monthly_pdf(db: Session, month_start: date, options: set[int], language: str = "ar", aggregate_leave: bool = False) -> bytes:
    """Render the approved monthly report: one PDF with the chosen sections.

    Options outside 1-11 are rejected by the caller. The month is explicit —
    no automatic period guessing — and nothing here schedules or sends anything.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    from app.reports import _pdf_cell, _pdf_font_name

    unknown = set(options) - MONTHLY_ALL_OPTIONS
    if unknown or not options:
        raise ValueError("خيارات التقرير الشهري غير صالحة.")
    month_start, month_end = monthly_date_bounds(month_start, options)

    def L(text):
        return _L(text, language)

    if language == "zh":
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from pathlib import Path
        font_name = "MYHotelMonthlyCJK"
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(font_name, str(Path(__file__).parent / "static/fonts/DroidSansFallbackFull.ttf")))
    else:
        font_name = _pdf_font_name()
    if language == "zh":
        arabic_font = _pdf_font_name()

        def cell_(value):
            import re
            from xml.sax.saxutils import escape
            text = "" if value is None else str(value)
            parts = re.split(r"([\u3000-\u303f\u3400-\u9fff\uff00-\uffef]+)", text)
            return "".join(escape(part) if re.fullmatch(r"[\u3000-\u303f\u3400-\u9fff\uff00-\uffef]+", part) else
                           '<font name="' + arabic_font + '">' + _pdf_cell(part) + '</font>'
                           for part in parts if part)
    else:
        cell_ = _pdf_cell
    base_styles = getSampleStyleSheet()
    title_style = ParagraphStyle("MRTitle", parent=base_styles["Title"], fontName=font_name, fontSize=16, leading=20, alignment=TA_RIGHT, spaceAfter=3 * mm)
    section_style = ParagraphStyle("MRSection", parent=base_styles["Heading2"], fontName=font_name, fontSize=13, leading=17, alignment=TA_RIGHT, spaceAfter=2 * mm)
    meta_style = ParagraphStyle("MRMeta", parent=base_styles["BodyText"], fontName=font_name, fontSize=9, leading=12, alignment=TA_RIGHT)
    cell_style = ParagraphStyle("MRCell", parent=base_styles["BodyText"], fontName=font_name, fontSize=7.5, leading=9.5, alignment=TA_RIGHT, wordWrap="CJK")
    header_style = ParagraphStyle("MRHeader", parent=cell_style, fontSize=8, leading=10, alignment=TA_CENTER, textColor=colors.white)

    page_size = landscape(A4)
    output = BytesIO()
    month_label_text = month_start.strftime("%m/%Y")
    document = SimpleDocTemplate(
        output,
        pagesize=page_size,
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"{L('التقرير الشهري')} {month_label_text}",
        author="M.Y. Business Hotel HR System",
    )
    available_width = page_size[0] - document.leftMargin - document.rightMargin
    story = [
        Paragraph(cell_(f"{L('التقرير الشهري')} — {month_label_text}"), title_style),
        Paragraph(cell_(f"{L('الفترة:')} {month_start.strftime('%d/%m/%Y')} {L('إلى')} {month_end.strftime('%d/%m/%Y')}"), meta_style),
        Paragraph(cell_(f"{L('الأقسام المختارة:')} " + "، ".join(L(MONTHLY_OPTION_LABELS[o]) for o in sorted(options) if o in MONTHLY_OPTION_LABELS)), meta_style),
        Spacer(1, 4 * mm),
    ]
    compare = 10 in options
    grouped = 11 in options
    first_section = True

    def render_result(result, approved_note: bool = False, extra_note: str | None = None):
        nonlocal first_section
        if not first_section:
            story.append(PageBreak())
        first_section = False
        story.append(Paragraph(cell_(" · ".join(L(part) for part in result.title.split(" · "))), section_style))
        story.append(Paragraph(cell_(f"{L(result.period_label)} · {L(result.scope_label)} · {L(result.status_label)} · {result.employee_count} {L('موظف')}"), meta_style))
        if approved_note:
            story.append(Paragraph(cell_(L("يعرض المعتمد فقط")), meta_style))
        if extra_note:
            story.append(Paragraph(cell_("\n".join(L(line) for line in extra_note.splitlines())), meta_style))
        story.append(Spacer(1, 2 * mm))
        groups = result.groups if grouped else _flatten_groups(result.groups)
        column_count = max(1, len(result.columns))
        table_rows = [[Paragraph(cell_(L(label)), header_style) for _, label, _ in result.columns]]
        for group in groups:
            dept_row = [Paragraph(cell_(L(group["department"])), cell_style)] + [Paragraph("", cell_style)] * (column_count - 1)
            table_rows.append(dept_row)
            for row in group["rows"]:
                table_rows.append([
                    Paragraph(cell_(_fmt_number(row.get(key)) if numeric else L(row.get(key, ""))), cell_style)
                    for key, _, numeric in result.columns
                ])
        if result.totals:
            totals_cells = []
            first = True
            for key, _, numeric in result.columns:
                if numeric and key in result.totals:
                    totals_cells.append(Paragraph(cell_(_fmt_number(result.totals[key])), cell_style))
                elif first:
                    totals_cells.append(Paragraph(cell_(L("الإجمالي")), cell_style))
                    first = False
                else:
                    totals_cells.append(Paragraph("", cell_style))
            table_rows.append(totals_cells)
        table = LongTable(table_rows, colWidths=[available_width / column_count] * column_count, repeatRows=1, hAlign="RIGHT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24415E")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(table)
        if result.summary:
            story.append(Spacer(1, 3 * mm))
            story.append(Paragraph(cell_(L("الملخص")), meta_style))
            summary_rows = [[Paragraph(cell_(L(label)), cell_style), Paragraph(cell_(L(str(value))), cell_style)] for label, value in result.summary]
            summary_table = LongTable(summary_rows, colWidths=[available_width * 0.3, available_width * 0.2], hAlign="RIGHT")
            summary_table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#FAF3E3")),
            ]))
            story.append(summary_table)
        if compare:
            if result.comparison and result.comparison.get("items"):
                story.append(Spacer(1, 3 * mm))
                story.append(Paragraph(cell_(f"{L('مقارنة بالشهر السابق')} ({result.comparison['period_label']})"), meta_style))
                comparison_rows = [[Paragraph(cell_(L(label)), header_style) for label in
                                    ("البند", "الشهر الحالي", "الشهر السابق", "التغير")]]
                for label, current, previous, change in result.comparison["items"]:
                    comparison_rows.append([Paragraph(cell_(L(label)), cell_style),
                                            Paragraph(cell_(_fmt_number(current)), cell_style),
                                            Paragraph(cell_(_fmt_number(previous)), cell_style),
                                            Paragraph(cell_(change or "—"), cell_style)])
                comparison_table = LongTable(comparison_rows, colWidths=[available_width * 0.25] * 4, hAlign="RIGHT")
                comparison_table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24415E")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]))
                story.append(comparison_table)
            else:
                story.append(Paragraph(cell_(L("مقارنة الشهر السابق غير متاحة لهذا القسم أو لا توجد بيانات سابقة قابلة للمقارنة.")), meta_style))
        if compare and result.comparison and result.comparison.get("like_for_like"):
            lfl = result.comparison["like_for_like"]
            story.append(Spacer(1, 3 * mm))
            lfl_heading = Paragraph(cell_(f"{L('مقارنة مثل بمثل مع الفترة السابقة')} ({result.comparison['period_label']})"), meta_style)
            lfl_rows = [
                [Paragraph(cell_(L("المستمرون في الفترتين")), cell_style), Paragraph(cell_(str(lfl["continuing_count"])), cell_style)],
                [Paragraph(cell_(L("صافي المستمرين — الفترة الحالية")), cell_style), Paragraph(cell_(_fmt_number(lfl["continuing_current_net"])), cell_style)],
                [Paragraph(cell_(L("صافي المستمرين — الفترة السابقة")), cell_style), Paragraph(cell_(_fmt_number(lfl["continuing_previous_net"])), cell_style)],
                [Paragraph(cell_(L("التغير مثل بمثل")), cell_style), Paragraph(cell_(lfl["continuing_pct"] or "—"), cell_style)],
            ]
            lfl_table = LongTable(lfl_rows, colWidths=[available_width * 0.3, available_width * 0.25], hAlign="RIGHT")
            lfl_table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#FAF3E3")),
            ]))
            story.append(KeepTogether([lfl_heading, lfl_table]))
        if result.warnings:
            story.append(Spacer(1, 2 * mm))
            for warning in result.warnings:
                story.append(Paragraph(cell_(f"{L('تنبيه:')} {_warning_text_ar(warning, language)}"), meta_style))

    for option in sorted(options):
        if option in (10, 11):
            continue
        if option in (8, 9):
            if not first_section:
                story.append(PageBreak())
            first_section = False
            report_type = MONTHLY_OPTION_TYPE[option]
            story.append(Paragraph(cell_(L(MONTHLY_OPTION_LABELS[option])), section_style))
            if report_type in (REPORT_TYPE_PAYROLL, REPORT_TYPE_ATTENDANCE):
                # Owner decision 2026-11-23: payroll and attendance sections of
                # the monthly report show approved data only; the note keeps
                # the signed document self-explanatory.
                story.append(Paragraph(cell_(L("يعرض المعتمد فقط")), meta_style))
            series = _monthly_six_month_series(db, month_start, report_type)
            if compare:
                story.append(Paragraph(cell_(L("هذا القسم يعرض اتجاه ستة أشهر؛ لا توجد مقارنة منفصلة بالشهر السابق.")), meta_style))
            if report_type == REPORT_TYPE_ATTENDANCE:
                absence_series = _monthly_six_month_series(db, month_start, "absence")
                story.append(_monthly_chart_drawing_grouped(
                    series, absence_series, "#14495c", "#a33b32",
                    L("الحضور"), L("الغياب"),
                ))
                story.append(Paragraph(cell_(L("الحضور (الشريط الأزرق): أيام بسجلات حضور معتمدة · الغياب (الشريط الأحمر): أيام غياب محتسبة في مسيرات رواتب معتمدة.")), meta_style))
                story.append(Paragraph(cell_(L("عدد الحضور هو عدد سجلات الموظف/اليوم المعتمدة ذات مدة عمل موجبة، وليس إثبات اكتمال سجل الشهر.")), meta_style))
                if any(v is None for _, v in absence_series):
                    story.append(Paragraph(cell_(L("شهر بلا مسيرة رواتب معتمدة لا يظهر له عمود غياب (فجوة وليست صفرًا).")), meta_style))
            else:
                color = "#8a6d1a"
                story.append(_monthly_chart_drawing(series, color))
                if all(value is None for _, value in series):
                    story.append(Paragraph(cell_(L("لا توجد بيانات معتمدة في هذه الفترة بعد.")), meta_style))
                elif any(value is None for _, value in series):
                    story.append(Paragraph(cell_(L("شهر بلا مسيرة رواتب معتمدة لا يظهر له عمود (فجوة وليست صفرًا).")), meta_style))
            continue
        if option == 7 and aggregate_leave:
            if not first_section:
                story.append(PageBreak())
            first_section = False
            story.append(Paragraph(cell_(L(MONTHLY_OPTION_LABELS[7])), section_style))
            story.append(Paragraph(cell_(L("كل حالات الإجازات حسب النوع والحالة — بدون بيانات الموظفين")), meta_style))
            story.append(Paragraph(cell_(L("أيام كل شهر تُعرض في شهرها، وأيام الخصم تُحسب مرة واحدة في شهر بداية الإجازة")), meta_style))
            story.append(Spacer(1, 2 * mm))
            rows = _monthly_leave_aggregate(db, month_start, month_end, all_statuses=True, language=language)
            header = [L("نوع الإجازة"), L("الحالة"), L("عدد الإجازات"), L("إجمالي الأيام"), L("أيام الخصم المسجلة")]
            table_rows = [[Paragraph(cell_(h), header_style) for h in header]]
            for row in rows:
                table_rows.append([
                    Paragraph(cell_(row["leave_type"]), cell_style),
                    Paragraph(cell_(L(LEAVE_STATUS_LABELS[row["status"]])), cell_style),
                    Paragraph(cell_(str(row["count"])), cell_style),
                    Paragraph(cell_(_fmt_number(row["days"])), cell_style),
                    Paragraph(cell_(_fmt_number(row["charged"])), cell_style),
                ])
            if len(table_rows) == 1:
                table_rows.append([Paragraph(cell_(L("لا توجد إجازات في هذا الشهر.")), cell_style)] + [Paragraph("", cell_style)] * 4)
            table = LongTable(table_rows, colWidths=[available_width * 0.28] + [available_width * 0.18] * 4, hAlign="RIGHT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24415E")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]))
            story.append(table)
            story.append(Paragraph(cell_(L("المسودة والملغاة معروضتان منفصلتين؛ أيام الخصم المسجلة ليست خصمًا منفذًا من الرصيد.")), meta_style))
            if compare:
                story.append(Paragraph(cell_(L("مقارنة الشهر السابق غير متاحة لهذا القسم أو لا توجد بيانات سابقة قابلة للمقارنة.")), meta_style))
            continue
        if option == 1:
            if not first_section:
                story.append(PageBreak())
            first_section = False
            summary = _payroll_month_summary(db, month_start, month_end)
            story.append(Paragraph(cell_(L(MONTHLY_OPTION_LABELS[1])), section_style))
            story.append(Paragraph(cell_(L("يعرض المعتمد فقط")), meta_style))
            story.append(Paragraph(cell_(f"{L('ملخص إجمالي للشهر — بدون تفاصيل الموظفين')}"), meta_style))
            story.append(Spacer(1, 2 * mm))
            if not summary["current_approved_runs"]:
                story.append(Paragraph(cell_(L("لا توجد مسيرة رواتب معتمدة أو مغلقة لهذا الشهر؛ القيم غير متاحة وليست صفرًا.")), meta_style))
            elif not summary["current_items"]:
                story.append(Paragraph(cell_(L("توجد مسيرة معتمدة أو مغلقة بلا بنود موظفين لهذا الشهر.")), meta_style))
            show_compare = compare and summary["previous"]["employees"] > 0
            header = [L("البند"), L("الشهر الحالي")] + ([L("الشهر السابق"), L("التغير")] if show_compare else [])
            table_rows = [[Paragraph(cell_(h), header_style) for h in header]]
            for label, key in (
                ("إجمالي الأساسي", "base"),
                ("البدلات والحوافز", "allowances"),
                ("الخصومات والجزاءات", "deductions"),
                ("السلف المخصومة", "advances"),
                ("صافي المرتبات", "net"),
            ):
                row = [Paragraph(cell_(L(label)), cell_style),
                       Paragraph(cell_(f"{_fmt_number(summary['current'][key])} {L('ج.م')}" if summary["current_approved_runs"] else "—"), cell_style)]
                if show_compare:
                    row.append(Paragraph(cell_(f"{_fmt_number(summary['previous'][key])} {L('ج.م')}"), cell_style))
                    row.append(Paragraph(cell_(str(_pct_change(summary['current'][key], summary['previous'][key]) or "—") if summary["current_approved_runs"] else "—"), cell_style))
                table_rows.append(row)
            row = [Paragraph(cell_(L("عدد الموظفين")), cell_style),
                   Paragraph(cell_(str(summary["current"]["employees"]) if summary["current_approved_runs"] else "—"), cell_style)]
            if show_compare:
                row.append(Paragraph(cell_(str(summary["previous"]["employees"])), cell_style))
                row.append(Paragraph(cell_(str(_pct_change(Decimal(summary["current"]["employees"]), Decimal(summary["previous"]["employees"])) or "—") if summary["current_approved_runs"] else "—"), cell_style))
            table_rows.append(row)
            col_count = len(header)
            table = LongTable(table_rows, colWidths=[available_width * 0.3] + [available_width * 0.18] * (col_count - 1), hAlign="RIGHT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24415E")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]))
            story.append(table)
            if grouped and summary["by_department"]:
                story.append(Spacer(1, 3 * mm))
                story.append(Paragraph(cell_(L("التوزيع حسب القسم — صافي المرتبات")), meta_style))
                dept_rows = [[Paragraph(cell_(L("القسم")), header_style),
                              Paragraph(cell_(L("صافي المرتبات")), header_style),
                              Paragraph(cell_(L("عدد الموظفين")), header_style)]]
                for row in summary["by_department"]:
                    dept_rows.append([
                        Paragraph(cell_(row["department"]), cell_style),
                        Paragraph(cell_(f"{_fmt_number(row['net'])} {L('ج.م')}"), cell_style),
                        Paragraph(cell_(str(row["employees"])), cell_style),
                    ])
                dept_table = LongTable(dept_rows, colWidths=[available_width * 0.4, available_width * 0.3, available_width * 0.2], hAlign="RIGHT")
                dept_table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24415E")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]))
                story.append(dept_table)
            if show_compare:
                story.append(Paragraph(cell_(f"{L('الفترة السابقة:')} {summary['period_label']}"), meta_style))
            elif compare:
                story.append(Paragraph(cell_(L("لا توجد مسيرات في الفترة السابقة للمقارنة.")), meta_style))
            continue
        selection = ReportSelection(
            report_type=MONTHLY_OPTION_TYPE[option],
            start_date=month_start,
            end_date=month_end,
            period_choice="custom",
            employee_status="all",  # monthly history is evidence-scoped, not today's active roster
            compare_like_for_like=compare,
            approved_only=MONTHLY_OPTION_TYPE[option] in (REPORT_TYPE_PAYROLL, REPORT_TYPE_ATTENDANCE),
        )
        selection.monthly_calendar_comparison = True
        if option in _MONTHLY_SECTION_FIELDS:
            selection.fields = set(_MONTHLY_SECTION_FIELDS[option])
        else:
            selection.fields = {"emp_name", "emp_code", "emp_department"}
        approved_note = MONTHLY_OPTION_TYPE[option] in (REPORT_TYPE_PAYROLL, REPORT_TYPE_ATTENDANCE)
        extra_note = None
        if MONTHLY_OPTION_TYPE[option] == REPORT_TYPE_LEAVE:
            selection.monthly_leave_clipping = True
            selection.monthly_leave_language = language
            extra_note = "أيام كل شهر تُعرض في شهرها، وأيام الخصم تُحسب مرة واحدة في شهر بداية الإجازة"
        if MONTHLY_OPTION_TYPE[option] == REPORT_TYPE_LEAVE:
            selection.employee_status = "all"  # leave overlap, never today's active-only filter
            for leave_status in ("confirmed", "draft", "cancelled"):
                selection.monthly_leave_status = leave_status
                result = build_report(db, selection)
                result.title += " · " + LEAVE_STATUS_LABELS[leave_status]
                result.status_label = "كل من له إجازة خلال الفترة مع حالته الحالية"
                render_result(result, extra_note="أيام كل شهر تُعرض في شهرها، وأيام الخصم تُحسب مرة واحدة في شهر بداية الإجازة" + "\n" + "المسودة والملغاة معروضتان منفصلتين؛ أيام الخصم المسجلة ليست خصمًا منفذًا من الرصيد.")
        else:
            result = build_report(db, selection)
            if selection.report_type == REPORT_TYPE_PAYROLL and not any(group["rows"] for group in result.groups):
                # No current approved item evidence cannot support a zero-cost comparison.
                result.comparison = None
                extra_note = "لا توجد بنود رواتب معتمدة أو مغلقة لهذا الشهر؛ لا تُفترض قيم صفرية للمقارنة."
            if selection.approved_only:
                # This selection deliberately cannot establish unresolved counts.
                result.summary = [(label, value) for label, value in result.summary if label != "أيام غير محسومة"]
                extra_note = "\n".join(filter(None, (extra_note, "هذا القسم يعرض المعتمد فقط ولا يقيس السجلات غير المحسومة.")))
            # Rows are already scoped by each section's actual period evidence.
            # Show current employment status without changing custom columns,
            # approved-only filters, record state, or historical employee data.
            result.columns.insert(
                next((i for i, (key, _, _) in enumerate(result.columns)
                      if not key.startswith("emp_")), len(result.columns)),
                ("employment_status", "حالة الموظف الحالية", False),
            )
            status_by_id = {employee.id: employee.status for employee in _scoped_employees(db, selection)}
            for group in result.groups:
                for row in group["rows"]:
                    row["employment_status"] = {"active": "نشط", "archived": "غادر"}.get(
                        status_by_id.get(row["employee_id"]), "حالة موظف غير معروفة")
            render_result(result, approved_note=approved_note, extra_note=extra_note)

    story.append(Spacer(1, 10 * mm))
    signature_rows = [[
        Paragraph(cell_(L("إعداد: ..............................")), meta_style),
        Paragraph(cell_(L("مراجعة: ..............................")), meta_style),
        Paragraph(cell_(L("اعتماد: ..............................")), meta_style),
    ]]
    signature_table = LongTable(signature_rows, colWidths=[available_width / 3] * 3, hAlign="RIGHT")
    signature_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    story.append(signature_table)

    generated = datetime.now(CAIRO_TZ).strftime("%d/%m/%Y %H:%M")
    footer_text = f"M.Y. Business Hotel — {L('استُخرج في:')} {generated}"
    document.build(story, canvasmaker=_NumberedCanvas.build(font_name, footer_text, language, cell_ if language == "zh" else None))
    return output.getvalue()
