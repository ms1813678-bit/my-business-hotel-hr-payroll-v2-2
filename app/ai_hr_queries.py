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
        "note_employee_not_found": "لم يطابق النص أو الرمز المحدد أي موظف. جرّب رمز الموظف أو الاسم وحده؛ لم يتم عرض بيانات موظفين آخرين.",
        "note_readonly_attendance": "تقرير مراجعة فقط. لم يتم اعتماد أو رفض أي سجل حضور.",
        "note_no_leave_types": "لا توجد أنواع إجازات مفعّلة في النظام.",
        "note_specify_employee": "حدد الموظف بالرمز (مثل MY-0001) أو بالاسم لعرض أرصدة الإجازات.",
        "note_readonly_leave": "أرصدة محسوبة من سجل الحركات فقط (قراءة).",
        "note_readonly_advances": "ملخص سلف نشطة/مدفوعة غير مغلقة بالكامل. قراءة فقط.",
        "note_readonly_staff": "ملخص جزاءات/مكافآت للفترة. قراءة فقط ولم يُعتمد أي إجراء من هنا.",
    },
    "en": {
        "note_readonly_employee": "Direct read from the local register. No data was changed.",
        "note_employee_not_found": "No employee matched the specified text or code. Try the employee code or name alone; no other employees' data is shown.",
        "note_readonly_attendance": "Review-only report. No attendance record was approved or rejected.",
        "note_no_leave_types": "No active leave types in the system.",
        "note_specify_employee": "Specify the employee by code (e.g. MY-0001) or name to show leave balances.",
        "note_readonly_leave": "Balances computed from the transaction ledger only (read-only).",
        "note_readonly_advances": "Summary of open active/paid advances. Read-only.",
        "note_readonly_staff": "Sanctions/rewards summary for the period. Read-only; nothing was approved here.",
    },
    "zh": {
        "note_readonly_employee": "直接读取本地记录，未修改任何数据。",
        "note_employee_not_found": "没有员工匹配指定的文字或编号。请尝试员工编号或仅输入姓名；未显示其他员工的数据。",
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
# No \b: CJK characters are word characters, so a boundary fails between
# Chinese text and a Latin code (查看MY-0001的). Lookarounds exclude
# ASCII alphanumerics instead.
_CODE_RE = re.compile(r"(?<![A-Za-z0-9])(MY[-\s]?\d{3,6}|SEC[-\s]?\d{1,4})(?!\d)", re.IGNORECASE)


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
    match = re.search(
        r"(?:للموظف|الموظف|موظف|للحارس|الحارس|حارس)\s+\S", q,
    )
    if not match:
        return False
    # Keep existing register quantifiers unchanged. Count questions use
    # a bare indefinite singular, never a definite employee/name selector.
    prefix = q[:match.start()]
    if re.search(r"(?:^|\s)(?:كل|أي|اي|جميع)\s+$", prefix):
        return False
    if (match.group(0).split()[0] in {"موظف", "حارس"}
            and re.search(r"(?:^|\s)(?:كام|كم)\s+$", prefix)):
        return False
    return True



_NAME_MATCH_LIMIT = 5
_LIKE_ESCAPE = "\\"


def _like_literal(value: str) -> str:
    """Escape LIKE metacharacters so the text matches literally (escape char first)."""
    return (value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
            .replace("%", _LIKE_ESCAPE + "%").replace("_", _LIKE_ESCAPE + "_"))


def _find_employees(db, question: str) -> list[Employee]:
    """Resolve employees by code or an explicit singular name reference."""
    return _find_employees_bounded(db, question)[0]


def _find_employees_bounded(db, question: str) -> tuple[list[Employee], bool]:
    """Like _find_employees, plus whether more name matches exist than the bound (never for a code)."""
    code_match = _CODE_RE.search(question or "")
    if code_match:
        raw = re.sub(r"\s+", "", code_match.group(1)).upper()
        if raw.startswith("MY") and not raw.startswith("MY-"):
            raw = "MY-" + raw[2:]
        if raw.startswith("SEC") and not raw.startswith("SEC-"):
            raw = "SEC-" + raw[3:]
        return list(db.scalars(select(Employee).where(Employee.code == raw)).all()), False

    q = " ".join((question or "").split())
    match = re.search(
        r"(?:للموظف|الموظف|موظف|للحارس|الحارس|حارس)\s+(.+)",
        q,
    )
    if not match:
        return [], False
    tail = match.group(1).strip(" :：-")
    tail = re.split(r"\d{4}-\d{2}", tail)[0].strip(" ؟?.,")
    if not tail or len(tail) < 2:
        return [], False
    # Same substring rule as before, but the typed text is literal ("%" and "_" are not wildcards) and the
    # bounded result is deterministic; one extra row tells whether it was truncated.
    rows = list(db.scalars(
        select(Employee)
        .where(Employee.full_name.ilike(f"%{_like_literal(tail)}%", escape=_LIKE_ESCAPE))
        .order_by(Employee.full_name, Employee.id)
        .limit(_NAME_MATCH_LIMIT + 1)
    ).all())
    return rows[:_NAME_MATCH_LIMIT], len(rows) > _NAME_MATCH_LIMIT


def is_attendance_analysis_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    if not q:
        return False
    attendance = any(term in q for term in ("حضور", "انصراف", "تأخير", "غياب", "اتأخر", "غايب", "ما حضرش", "محضرش", "attendance", "absent", "late", "考勤", "出勤", "迟到", "缺席", "没来"))
    analysis = any(
        term in q
        for term in (
            "تحليل", "حلّل", "حلل", "ملخص", "ملاحظات", "مراجعة",
            "مشاكل", "تقرير", "summary", "analyze", "analysis", "review", "report", "分析", "汇总", "问题",
            "مين", "من الغائب", "كام", "who", "how many", "谁", "多少", "几个",
        )
    )
    if "أمن" in q and "بصم" in q:
        return False
    # Negated absence ("who is NOT absent") is a different question the
    # record-based analysis cannot answer; decline instead of reusing rows.
    negated_absence = any(term in q for term in (
        "مش غايب", "مش غائب", "ما غابش", "ماغابش", "موجود النهارده", "حضر النهارده",
        "not absent", "did not miss", "didn't miss", "came today", "present today",
        "没有缺席", "没缺席", "今天来了",
    ))
    if negated_absence:
        return False
    return attendance and analysis


def is_leave_balance_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    return any(term in q for term in (
        "رصيد إجازة", "رصيد اجازة", "أرصدة الإجازات", "ارصدة الاجازات", "إجازات الموظف", "اجازات الموظف",
        "كام يوم إجازة", "كم يوم إجازة", "فاضل من إجازاته", "باقي من إجازاته", "إجازاته الباقية",
        "leave balance", "leave days left", "remaining leave", "رصيد", "请假余额", "假期余额", "年假余额", "还剩多少假",
    ))


def is_advance_query_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    return any(term in q for term in (
        "سلف", "سلفة", "أقساط", "اقساط", "قسط", "advance", "installment", "预支", "借款", "分期",
        "عليه سلفة", "متبقي من السلفة", "باقي من السلفة",
    ))


def is_pre_payroll_review_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    if not q:
        return False
    return any(term in q for term in (
        "مراجعة ما قبل الرواتب", "مراجعة قبل اعتماد الرواتب", "قبل اعتماد الرواتب",
        "قبل اعتماد المرتبات", "راجع الرواتب قبل", "مراجعة الرواتب قبل",
        "pre-payroll", "pre payroll review", "payroll review before",
        "发薪前检查", "工资审批前检查", "工资审核前",
    ))


def is_staff_action_query_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    return any(
        term in q
        for term in ("جزاء", "جزاءات", "مكافأة", "مكافات", "مكافآت", "عقوبة", "خصم تأديبي",
                     "اتجازى", "اتعاقب", "عليه جزاء", "اتكافأ",
                     "sanction", "reward", "penalty", "奖惩", "处罚", "奖励", "被处罚")
    )


def is_employee_query_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    return any(term in q for term in (
        "بيانات الموظف", "تفاصيل الموظف", "سجل الموظف", "ابحث عن الموظف",
        "بيانات العامل", "هات بيانات", "عايز بيانات", "عاوز بيانات", "دور على موظف", "دوّر على موظف",
        "employee profile", "employee details", "find employee", "look up employee",
        "员工信息", "员工详情", "员工资料", "查一下员工",
    ))


_COUNT_DOMAIN_TERMS = (
    "حضور", "انصراف", "تأخير", "اتأخر", "غياب", "غايب", "بصم", "وردية", "شيفت",
    "إجاز", "اجاز", "سلف", "مرتب", "راتب", "رواتب", "جزاء", "مكاف",
    "attendance", "absent", "late", "leave", "vacation", "advance",
    "salary", "payroll", "sanction", "reward", "punch", "shift",
    "考勤", "出勤", "迟到", "缺席", "请假", "预支", "工资", "薪资", "处罚", "奖励", "打卡", "班次",
)


def is_employee_count_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    count_word = any(term in q for term in (
        "عدد", "كام", "كم", "how many", "number of", "count", "多少", "几个", "几位",
    ))
    employee_word = any(term in q for term in (
        "موظف", "عامل", "شغال", "employee", "staff", "员工", "职员",
    ))
    if not (count_word and employee_word):
        return False
    # A historical or dated count is a different question (we only know the
    # current register); decline instead of answering with today's numbers.
    if re.search(r"(19|20)\d{2}", q) or any(
        term in q for term in (
            "السنة اللي فاتت", "السنة الماضية", "last year", "في 20", "去年",
            "امبارح", "أمس", "امس", "yesterday", "昨天", "昨日",
            "بكرة", "بكره", "غدا", "غدًا", "tomorrow", "明天",
            "الشهر الماضي", "الشهر اللي فات", "last month", "上个月", "上月",
            "الأسبوع الماضي", "الاسبوع الماضي", "last week", "上周",
        )
    ):
        return False
    # A count question qualified by a domain (late today, on leave, with an
    # advance...) asks about that domain's records, never the headcount.
    return not any(term in q for term in _COUNT_DOMAIN_TERMS)


_INACTIVE_TERMS = (
    "غير نشط", "غير فعال", "مش نشط", "موقوف", "ساب الشغل", "ترك العمل", "انتهت خدمته",
    "inactive", "not active", "non-active", "terminated", "former",
    "离职", "已离职", "不在职", "非在职", "不在岗",
)

_DEPARTMENT_TERMS = ("قسم", "department", "部门")


def _selection(truncated: bool) -> dict[str, Any]:
    """Metadata every name-selected report carries: more employees matched than the bound."""
    return {"limit": _NAME_MATCH_LIMIT, "truncated": bool(truncated)}


def _selection_warning(report: dict[str, Any], language: str) -> list[str]:
    if not (report.get("selection") or {}).get("truncated"):
        return []
    return [_msg(language,
        f"النتائج تخص أول {_NAME_MATCH_LIMIT} موظفين مطابقين فقط؛ يوجد موظفون آخرون مطابقون. استخدم كود الموظف أو اسمًا أدق.",
        f"Results cover only the first {_NAME_MATCH_LIMIT} matching employees; more employees match. Use the employee code or a more specific name.",
        f"结果仅包含前 {_NAME_MATCH_LIMIT} 名匹配员工；还有更多员工匹配。请使用员工编号或更具体的姓名。")]


def build_employee_count_report(question: str = "") -> dict[str, Any]:
    q = " ".join((question or "").casefold().split())
    with SessionLocal() as db:
        base = select(func.count(Employee.id))
        active = db.scalar(base.where(Employee.status == "active")) or 0
        total = db.scalar(select(func.count(Employee.id))) or 0
        inactive = total - int(active)
        departments = db.scalars(
            select(Department).where(Department.active.is_(True))
        ).all()
        # A registered department name scopes the count even without the
        # literal word "قسم/department/部门"; the marker without a match is
        # an honest decline, never a substituted global number.
        matched = [d for d in departments if d.name and d.name.casefold() in q]
        wants_inactive = any(term in q for term in _INACTIVE_TERMS)
        if matched:
            department = matched[0]
            if wants_inactive:
                dept_inactive = db.scalar(
                    select(func.count(Employee.id)).where(
                        Employee.status != "active",
                        Employee.department_id == department.id,
                    )
                ) or 0
                return {
                    "scope": "department_inactive", "department": department.name,
                    "inactive": int(dept_inactive),
                }
            dept_active = db.scalar(
                select(func.count(Employee.id)).where(
                    Employee.status == "active",
                    Employee.department_id == department.id,
                )
            ) or 0
            return {
                "scope": "department", "department": department.name,
                "active": int(dept_active),
            }
        if any(term in q for term in _DEPARTMENT_TERMS):
            return {"scope": "department_unknown"}
        # A scoped count whose scope matches nothing registered declines or
        # clarifies — never answers with the global number.
        scope_match = re.search(r"(?:في|in|at)\s+([^\s؟?,.،]+)", q)
        if scope_match:
            candidate = scope_match.group(1)
            whole_org = (
                "فندق", "الفندق", "hotel", "شركة", "الشركة", "company", "هنا",
                "عندنا", "here", "المكان", "الشغل", "العمل", "work", "المحل",
            )
            status_words = _INACTIVE_TERMS + ("نشط", "النشطين", "active", "شغال", "شغالين", "working", "在职", "工作")
            if candidate not in whole_org and candidate not in status_words:
                return {
                    "scope": "scope_unknown", "candidate": candidate,
                    "known": [d.name for d in departments if d.name],
                }
    if wants_inactive:
        return {"scope": "inactive", "inactive": int(inactive), "total": int(total)}
    return {"scope": "all", "active": int(active), "total": int(total)}


def format_employee_count_report(report: dict[str, Any], language: str = "ar") -> str:
    scope = report.get("scope", "all")
    if scope == "department_unknown":
        return _msg(
            language,
            "لم أتعرّف على القسم المطلوب بالاسم في سؤالك؛ الأقسام المسجلة تظهر في صفحة الموظفين.",
            "I could not recognize the department named in your question; the registered departments are listed on the Employees page.",
            "无法识别您问题中的部门名称；已登记的部门请在员工页面查看。",
        )
    if scope == "scope_unknown":
        known_ar = "، ".join(report.get("known") or []) or "—"
        known_en = ", ".join(report.get("known") or []) or "—"
        return _msg(
            language,
            f"لم أتعرّف على نطاق «{report['candidate']}». الأقسام المسجلة: {known_ar}. حدد القسم باسمه المسجل أو اسأل عن العدد الإجمالي.",
            f"I could not recognize the scope '{report['candidate']}'. Registered departments: {known_en}. Use a registered department name or ask for the total count.",
            f"无法识别范围「{report['candidate']}」。已登记的部门：{known_en}。请使用已登记的部门名称，或询问总人数。",
        )
    if scope == "department_inactive":
        return _msg(
            language,
            f"الموظفون غير النشطين في قسم {report['department']} حاليًا: {report['inactive']}.",
            f"Inactive employees in the {report['department']} department right now: {report['inactive']}.",
            f"{report['department']} 部门当前非在职员工数：{report['inactive']}。",
        )
    if scope == "department":
        return _msg(
            language,
            f"عدد الموظفين النشطين في قسم {report['department']} حاليًا: {report['active']}.",
            f"Active employees in the {report['department']} department right now: {report['active']}.",
            f"{report['department']} 部门当前在职员工数：{report['active']}。",
        )
    if scope == "inactive":
        return _msg(
            language,
            f"الموظفون غير النشطين حاليًا: {report['inactive']} (إجمالي المسجلين: {report['total']}).",
            f"Inactive employees right now: {report['inactive']} (total on record: {report['total']}).",
            f"当前非在职员工数：{report['inactive']}（登记总数：{report['total']}）。",
        )
    return _msg(
        language,
        f"عدد الموظفين النشطين حاليًا: {report['active']} (إجمالي المسجلين: {report['total']}).",
        f"Active employees right now: {report['active']} (total on record: {report['total']}).",
        f"当前在职员工数：{report['active']}（登记总数：{report['total']}）。",
    )


def is_payroll_query_request(question: str) -> bool:
    q = " ".join((question or "").casefold().split())
    return any(term in q for term in (
        "راتب الموظف", "رواتب الموظفين", "مسير الرواتب", "تفاصيل الراتب",
        "صافي الراتب", "مرتبه كام", "مرتب فلان", "صافي مرتب", "مرتبه كام الشهر",
        "payroll", "salary details", "net salary", "how much salary",
        "工资批次", "工资明细", "薪资", "实发工资", "工资多少",
    ))


def build_employee_report(question: str) -> dict[str, Any]:
    with SessionLocal() as db:
        employees, truncated = _find_employees_bounded(db, question)
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
    return {"items": items, "note": "note_readonly_employee", "selection": _selection(truncated)}


def format_employee_report(report: dict[str, Any], language: str = "ar") -> str:
    lines = [
        _msg(language, "نتيجة البحث في سجل الموظفين.", "Employee register search result.", "员工登记查询结果。"),
        _NOTE[language if language in _NOTE else "ar"][report.get("note", "note_readonly_employee")],
        *_selection_warning(report, language),
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
        employees, truncated = _find_employees_bounded(db, question)
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
                PayrollPayment.payroll_item_id == item.id,
                # Superseded payments were corrected away; only active records count,
                # matching payment_totals() everywhere else.
                PayrollPayment.superseded_by_payment_id.is_(None),
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
                "items": rows, "selection": _selection(truncated)}


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
    lines.extend(_selection_warning(report, language))
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
        is_employee_count_request(question)
        or is_employee_query_request(question)
        or is_payroll_query_request(question)
        or is_attendance_analysis_request(question)
        or is_leave_balance_request(question)
        or is_advance_query_request(question)
        or is_staff_action_query_request(question)
        or is_pre_payroll_review_request(question)
        or _is_weekly_summary(question)
        or _usage_guide_topic(question) is not None
    )


def _classify_attendance_row(record: AttendanceDailyRecord) -> list[tuple[str, object]]:
    """Structured flags; localized at display time, never stored."""
    notes: list[tuple[str, object]] = []
    if record.status in {"needs_review", "ready_for_review"}:
        notes.append(("record_status", record.status))
    # Late/early use payroll's exact rule: naive wall-clock subtraction on
    # payroll's endpoint precedence (approved correction, then fingerprint,
    # then program). Raw punches stay visible as separate evidence fields;
    # these flags explain what payroll will actually charge.
    actual_start = record.approved_clock_in or record.first_clock_in or record.program_clock_in
    actual_end = record.approved_clock_out or record.last_clock_out or record.program_clock_out
    if record.scheduled_start_at and actual_start:
        late = max(0, int((actual_start - record.scheduled_start_at).total_seconds() // 60))
        if late > 0:
            notes.append(("late_in", late))
    if record.scheduled_end_at and actual_end:
        early = max(0, int((record.scheduled_end_at - actual_end).total_seconds() // 60))
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


def build_attendance_analysis(*, work_date: date | None, month_start: date | None, question: str = "") -> dict[str, Any]:
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

        # Explicit individual requests never fall back to other employees.
        # General/plural or quantified-register questions remain unfiltered.
        selection_truncated = False
        if question and _employee_filter_requested(question):
            employees, selection_truncated = _find_employees_bounded(db, question)
            if not employees:
                return {
                    "period": period_label,
                    "day_mode": work_date is not None,
                    "reviewed_rows": 0,
                    "flagged_rows": 0,
                    "items": [],
                    "note": "note_employee_not_found",
                    "selection": {"limit": _NAME_MATCH_LIMIT, "truncated": False},
                }
            query = query.where(AttendanceDailyRecord.employee_id.in_(
                [employee.id for employee in employees]))
        rows = db.execute(query.order_by(AttendanceDailyRecord.work_date, Employee.code)).all()
        late_count = 0
        late_covered_rows = 0
        late_details: list[dict[str, Any]] = []
        late_missing: list[dict[str, Any]] = []
        if work_date is not None:
            for record, employee in rows:
                start = record.scheduled_start_at
                # Same endpoint precedence payroll and the row classifier use:
                # approved correction, then fingerprint, then program.
                if record.approved_clock_in:
                    clock, source = record.approved_clock_in, "approved"
                elif record.first_clock_in:
                    clock, source = record.first_clock_in, "fingerprint"
                else:
                    clock, source = record.program_clock_in, "program"
                if start and clock:
                    late_covered_rows += 1
                    # Shared rule with _classify_attendance_row / payroll:
                    # whole late minutes floored; sub-minute drift is not late.
                    late_minutes = max(0, int((clock - start).total_seconds() // 60))
                    if late_minutes > 0:
                        late_count += 1
                        late_details.append({
                            "employee_code": employee.code,
                            "employee_name": employee.full_name,
                            "work_date": record.work_date.isoformat(),
                            "late_minutes": late_minutes,
                            "source": source,
                        })
                else:
                    # Visible data gap: this row cannot be judged late or not.
                    late_missing.append({
                        "employee_code": employee.code,
                        "employee_name": employee.full_name,
                        "gap": "no_schedule" if not start else "no_clock",
                    })
        late_determinable = late_covered_rows > 0
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
            "day_mode": work_date is not None,
            "reviewed_rows": len(rows),
            "flagged_rows": len(items),
            "items": items,
            "note": "note_readonly_attendance",
            "late_count": late_count,
            "late_determinable": late_determinable,
            "late_covered_rows": late_covered_rows,
            "late_total_rows": len(rows),
            "late_details": late_details,
            "late_missing": late_missing,
            "selection": _selection(selection_truncated),
        }


def format_attendance_analysis(report: dict[str, Any], language: str = "ar") -> str:
    lang = language if language in _NOTE else "ar"
    if report.get("note") == "note_employee_not_found":
        return "\n".join([
            _msg(language, f"تحليل حضور للفترة {report['period']}.",
                 f"Attendance analysis for {report['period']}.", f"{report['period']} 考勤分析。"),
            _NOTE[lang]["note_employee_not_found"],
        ])
    lines = [
        _msg(language, f"تحليل حضور للفترة {report['period']}.",
             f"Attendance analysis for {report['period']}.", f"{report['period']} 考勤分析。"),
        _NOTE[lang][report.get("note", "note_readonly_attendance")],
        *_selection_warning(report, language),
        _msg(language,
             f"سجلات مفحوصة: {report['reviewed_rows']} — تحتاج انتباه: {report['flagged_rows']}.",
             f"Rows checked: {report['reviewed_rows']} — need attention: {report['flagged_rows']}.",
             f"已检查记录：{report['reviewed_rows']} — 需要注意：{report['flagged_rows']}。"),
    ]
    if report.get("day_mode"):
        # Honest coverage: a day analysis sees entered records only. It cannot
        # tell whether someone with no record was absent or just not entered.
        lines.append(_msg(language,
            "التغطية: التحليل يشمل السجلات المدخلة لهذا اليوم فقط؛ لا يمكن الجزم بغياب من لا سجل له — قد يكون غير مسجل أصلًا.",
            "Coverage: this analysis reads only the records entered for this day; it cannot determine whether someone with no record was absent or simply not entered.",
            "覆盖范围：此分析仅读取当日已录入的记录；无法判断没有记录的人是缺勤还是尚未录入。"))
        if report.get("late_determinable"):
            covered = report.get("late_covered_rows", 0)
            total_rows = report.get("late_total_rows", covered)
            if covered < total_rows:
                lines.append(_msg(language,
                    f"المتأخرون عن موعد بداية الوردية: {report['late_count']} (من أصل {covered} سجلًا فيه بصمة؛ {total_rows - covered} سجلًا بدون بصمة لا يشملها العد).",
                    f"Late after scheduled shift start: {report['late_count']} (out of {covered} records with a clock-in; {total_rows - covered} records without one are not counted).",
                    f"晚于排班开始时间到岗：{report['late_count']} 人（在 {covered} 条有打卡记录中；另有 {total_rows - covered} 条无打卡记录未计入）。"))
            else:
                lines.append(_msg(language,
                    f"المتأخرون عن موعد بداية الوردية: {report['late_count']}.",
                    f"Late after scheduled shift start: {report['late_count']}.",
                    f"晚于排班开始时间到岗：{report['late_count']} 人。"))
        else:
            lines.append(_msg(language,
                "لا يمكن تحديد عدد المتأخرين: لا توجد بصمات ومواعيد كافية في سجلات هذا اليوم.",
                "The late count cannot be determined: not enough clock-ins and schedules in this day's records.",
                "无法确定迟到人数：当日记录中缺少足够的打卡和排班数据。"))

    if report.get("day_mode"):
        # Drill-down: tie every counted number to its source records, and
        # make rows that cannot be judged visible instead of silently skipped.
        details = report.get("late_details") or []
        if details:
            lines.append(_msg(language,
                "تفاصيل المتأخرين (الاسم والدقائق ومصدر الاحتساب):",
                "Late records behind the count (name, minutes, clock source):",
                "迟到计数对应的明细（姓名、分钟数、打卡来源）："))
            for index, item in enumerate(details[:20], start=1):
                source_label = _msg(language,
                    {"approved": "تصحيح معتمد", "fingerprint": "البصمة", "program": "برنامج المواعيد"}[item["source"]],
                    {"approved": "approved correction", "fingerprint": "fingerprint", "program": "schedule program"}[item["source"]],
                    {"approved": "已批准的修正", "fingerprint": "指纹打卡", "program": "排班系统"}[item["source"]])
                lines.append(
                    f"{index}) {item['employee_code']} — {item['employee_name']} — "
                    + _msg(language, f"تأخر {item['late_minutes']} دقيقة",
                           f"{item['late_minutes']} min late", f"迟到 {item['late_minutes']} 分钟")
                    + f" — {source_label}"
                )
            if len(details) > 20:
                lines.append(_msg(language,
                    f"... و{len(details) - 20} سجل تأخير إضافي. راجع شاشة مراجعة الحضور.",
                    f"... and {len(details) - 20} more late records. See the attendance review screen.",
                    f"……另有 {len(details) - 20} 条迟到记录。请查看考勤审核页面。"))
        missing = report.get("late_missing") or []
        if missing:
            no_clock = [m for m in missing if m["gap"] == "no_clock"]
            no_schedule = [m for m in missing if m["gap"] == "no_schedule"]
            lines.append(_msg(language,
                f"سجلات لا يمكن الحكم على تأخيرها (خارج العد): {len(missing)} — بدون بصمة: {len(no_clock)}، بدون موعد مجدول: {len(no_schedule)}.",
                f"Records that cannot be judged for lateness (not counted): {len(missing)} — no clock-in: {len(no_clock)}, no scheduled start: {len(no_schedule)}.",
                f"无法判断迟到的记录（未计入）：{len(missing)} 条 — 无打卡：{len(no_clock)} 条，无排班：{len(no_schedule)} 条。"))
            for item in missing[:10]:
                gap_label = _msg(language,
                    "لا يوجد موعد مجدول" if item["gap"] == "no_schedule" else "لا توجد بصمة",
                    "no scheduled start" if item["gap"] == "no_schedule" else "no clock-in",
                    "无排班时间" if item["gap"] == "no_schedule" else "无打卡记录")
                lines.append(f"   - {item['employee_code']} — {item['employee_name']} — {gap_label}")
            if len(missing) > 10:
                lines.append(_msg(language,
                    f"... و{len(missing) - 10} سجلًا إضافيًا غير قابل للحكم.",
                    f"... and {len(missing) - 10} more undecidable records.",
                    f"……另有 {len(missing) - 10} 条无法判断的记录。"))
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
        employees, truncated = _find_employees_bounded(db, question)
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
        return {"employees": result, "note": "note_readonly_leave", "selection": _selection(truncated)}


def format_leave_balance(report: dict[str, Any], language: str = "ar") -> str:
    lang = language if language in _NOTE else "ar"
    lines = [_NOTE[lang][report.get("note", "note_readonly_leave")], *_selection_warning(report, language)]
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
        employees, truncated = _find_employees_bounded(db, question)
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
            "selection": _selection(truncated),
        }


def format_advance_report(report: dict[str, Any], language: str = "ar") -> str:
    lang = language if language in _NOTE else "ar"
    lines = [_NOTE[lang][report.get("note", "note_readonly_advances")], *_selection_warning(report, language)]
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
        employees, truncated = _find_employees_bounded(db, question)
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
            "selection": _selection(truncated),
        }


def format_staff_actions(report: dict[str, Any], language: str = "ar") -> str:
    lang = language if language in _NOTE else "ar"
    lines = [
        _msg(language, f"جزاءات ومكافآت لشهر {report['month']}.",
             f"Sanctions and rewards for {report['month']}.", f"{report['month']} 奖惩汇总。"),
        _NOTE[lang][report.get("note", "note_readonly_staff")],
        *_selection_warning(report, language),
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


def _usage_guide_topic(question: str):
    from app.usage_guide import is_usage_guide_request

    return is_usage_guide_request(question)


def _is_weekly_summary(question: str) -> bool:
    from app.weekly_summary import is_weekly_summary_request

    return is_weekly_summary_request(question)


_READ_ANCHOR_TERMS = (
    "رصيد", "أرصدة", "ارصدة", "balance", "余额",
    "تحليل", "حلل", "حلّل", "analysis", "analyze", "分析",
    "ملخص", "summary", "汇总", "تقرير", "report", "报告",
    "بيانات", "عرض", "اشوف", "أشوف", "view", "check", "查看", "查询",
    "كام", "كم عدد", "كم موظف", "how many", "多少", "几个",
    "مين", "who", "谁",
)


def _has_read_anchor(question: str) -> bool:
    # A code identifies WHO the question is about, not whether it is a data
    # read or a how-to ("add an advance for MY-0001" is still a how-to).
    q = " ".join((question or "").casefold().split())
    return any(term in q for term in _READ_ANCHOR_TERMS)


def explicit_unsupported_answer(question: str, language: str = "ar") -> str | None:
    """Deterministic honesty for recognized-but-unsupported questions.

    Checked before any model call and in the offline fallback, so a
    historical or dated count is explicitly declined everywhere instead of
    being guessed from current metrics.
    """
    q = " ".join((question or "").casefold().split())
    if not q:
        return None
    count_like = any(term in q for term in (
        "كام موظف", "كم موظف", "عدد الموظفين", "كام واحد", "كم عدد",
        "how many employees", "how many staff", "number of employees",
        "多少员工", "几位员工", "员工数量", "在职员工",
    ))
    dated = bool(re.search(r"(19|20)\d{2}", q)) or any(term in q for term in (
        "امبارح", "أمس", "امس", "yesterday", "昨天", "昨日",
        "بكرة", "بكره", "غدا", "غدًا", "tomorrow", "明天",
        "last year", "السنة الماضية", "السنة اللي فاتت", "去年",
        "last month", "الشهر الماضي", "الشهر اللي فات", "上个月", "上月",
        "last week", "الأسبوع الماضي", "الاسبوع الماضي", "上周",
        "بتاريخ", "في تاريخ",
    ))
    if count_like and dated:
        return _msg(
            language,
            "لا أستطيع تحديد عدد الموظفين في تاريخ أو فترة سابقة من السجلات الحالية؛ الأعداد المتاحة لدي هي الحالية فقط. يمكنك سؤالي عن العدد الحالي أو عن حضور يوم محدد.",
            "I cannot determine the employee count for a past or specific date from the current records; only the current count is available to me. You can ask about the current count or about a specific day's attendance.",
            "我无法根据现有记录确定过去或指定日期的员工人数；我只能提供当前人数。您可以询问当前人数或某一天的考勤情况。",
        )
    negated_absence = any(term in q for term in (
        "مش غايب", "مش غائب", "ما غابش", "ماغابش", "موجود النهارده", "حضر النهارده",
        "not absent", "did not miss", "didn't miss", "came today", "present today",
        "没有缺席", "没缺席", "今天来了",
    ))
    if negated_absence:
        return _msg(
            language,
            "لا أستطيع تحديد من ليس غائبًا: السجلات المتاحة تُظهر سجلات الحضور المدخلة فقط ولا تثبت حضور من لا سجل له. يمكنك سؤالي عن تحليل حضور يوم محدد أو عن السجلات التي تحتاج مراجعة.",
            "I cannot determine who is not absent: the available records only show entered attendance records and cannot prove the presence of someone with no record. You can ask me for a specific day's attendance analysis or for records that need review.",
            "我无法确定谁没有缺席：现有记录仅显示已录入的考勤记录，无法证明没有记录的人出勤。您可以询问某一天的考勤分析或需要审核的记录。",
        )
    return None


def answer_structured_hr_query(question: str, language: str = "ar") -> str:
    """Dispatch a natural-language HR question to the matching read-only report."""
    if _is_weekly_summary(question):
        from app.weekly_summary import build_weekly_summary, format_weekly_summary

        return format_weekly_summary(build_weekly_summary(), language)
    topic = _usage_guide_topic(question)
    data_read = (
        is_employee_count_request(question) or is_employee_query_request(question)
        or is_pre_payroll_review_request(question) or is_payroll_query_request(question)
        or is_attendance_analysis_request(question) or is_leave_balance_request(question)
        or is_advance_query_request(question) or is_staff_action_query_request(question)
    )
    # A how-to question stays a guide answer unless it carries a real data
    # anchor (an employee code, a count word, or an explicit read noun) —
    # "how do I add an advance" must never return advance records.
    if topic is not None and not (data_read and _has_read_anchor(question)):
        from app.usage_guide import format_usage_guide

        return format_usage_guide(topic, language)
    if is_employee_count_request(question):
        return format_employee_count_report(build_employee_count_report(question), language)
    if is_employee_query_request(question):
        return format_employee_report(build_employee_report(question), language)
    if is_pre_payroll_review_request(question):
        from app.pre_payroll_review import build_pre_payroll_review, format_pre_payroll_review
        month = _parse_month(question) or date.today().replace(day=1)
        return format_pre_payroll_review(build_pre_payroll_review(month), language)
    if is_payroll_query_request(question):
        return format_payroll_report(build_payroll_report(question), language)
    if is_attendance_analysis_request(question):
        q = question or ""
        normalized = " ".join(q.casefold().split())
        is_month = "شهر" in normalized or "month" in normalized or "月份" in normalized
        asks_today = any(term in normalized for term in ("النهارده", "النهاردة", "اليوم", "today", "今天", "今日"))
        asks_yesterday = any(term in normalized for term in ("امبارح", "أمس", "امس", "yesterday", "昨天"))
        asks_tomorrow = any(term in normalized for term in ("بكرة", "بكره", "غدا", "غدًا", "tomorrow", "明天"))
        if asks_today and not is_month:
            report = build_attendance_analysis(work_date=date.today(), month_start=None, question=q)
        elif asks_yesterday and not is_month:
            report = build_attendance_analysis(
                work_date=date.today() - timedelta(days=1), month_start=None, question=q)
        elif asks_tomorrow and not is_month:
            report = build_attendance_analysis(
                work_date=date.today() + timedelta(days=1), month_start=None, question=q)
        elif _DATE_RE.search(q) and "شهر" not in q:
            report = build_attendance_analysis(work_date=_parse_date(q), month_start=None, question=q)
        else:
            report = build_attendance_analysis(work_date=None, month_start=_parse_month(q), question=q)
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
