"""On-demand weekly work summary: read-only counts over a 7-day window.

Pure reads, deterministic counts, no business rules of its own. The report
is advisory: it tells the HR admin what happened and what is waiting, it
never changes or recommends changes to data.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import (
    AdvanceRequest, AttendanceDailyRecord, Employee, LeaveRecord,
    PayrollRun, StaffAction,
)


def is_weekly_summary_request(question: str) -> bool:
    """Detect natural-language requests for the weekly work summary."""
    q = " ".join((question or "").casefold().split())
    if not q:
        return False
    phrases = (
        "ملخص الأسبوع", "الملخص الأسبوعي", "ملخص أسبوعي", "ملخص الاسبوع",
        "weekly summary", "summary of the week", "this week's summary",
        "this week summary", "week summary",
        "本周总结", "每周总结", "本周摘要", "周度总结", "本周发生了什么", "这周怎么样", "本周情况",
    ) + (
        "ايه اللي حصل الاسبوع", "إيه اللي حصل الأسبوع", "اللي حصل الاسبوع",
        "أخبار الأسبوع", "اخبار الاسبوع", "شغل الأسبوع ده", "الاسبوع ده ايه",
        "what happened this week", "this week at work", "how was this week",
    )
    return any(phrase in q for phrase in phrases)


def build_weekly_summary(end: date | None = None, days: int = 7) -> dict[str, Any]:
    """Collect read-only counts for the window (end-days+1 .. end)."""
    end = end or date.today()
    start = end - timedelta(days=days - 1)
    with SessionLocal() as db:
        def count(query) -> int:
            return db.scalar(query) or 0

        attendance_review = count(select(func.count(AttendanceDailyRecord.id)).where(
            AttendanceDailyRecord.work_date.between(start, end),
            AttendanceDailyRecord.status.in_(["needs_review", "ready_for_review"]),
        ))
        attendance_approved = count(select(func.count(AttendanceDailyRecord.id)).where(
            AttendanceDailyRecord.work_date.between(start, end),
            AttendanceDailyRecord.status == "approved",
        ))
        leaves_by_status = dict(db.execute(select(
            LeaveRecord.status, func.count(LeaveRecord.id)
        ).where(LeaveRecord.start_date <= end, LeaveRecord.end_date >= start)
          .group_by(LeaveRecord.status)).all())
        advances_by_status = dict(db.execute(select(
            AdvanceRequest.status, func.count(AdvanceRequest.id)
        ).where(AdvanceRequest.request_date.between(start, end))
          .group_by(AdvanceRequest.status)).all())
        # Bucket staff actions by the Cairo calendar day (hotel local time).
        # created_at is stored in UTC; a Cairo day does not align with the
        # database/server day, so convert in Python for identical behavior
        # on every database. Naive values are read as UTC (models store UTC).
        cairo = ZoneInfo("Africa/Cairo")
        staff_rows = db.scalars(select(StaffAction).where(
            StaffAction.created_at >= datetime.combine(
                start - timedelta(days=1), datetime.min.time()),
            StaffAction.created_at <= datetime.combine(
                end + timedelta(days=1), datetime.max.time()),
        )).all()

        def _cairo_day(ts: datetime) -> date:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.astimezone(cairo).date()

        staff_actions = sum(
            1 for row in staff_rows if start <= _cairo_day(row.created_at) <= end
        )
        hires = count(select(func.count(Employee.id)).where(
            Employee.hire_date.between(start, end),
        ))
        latest_run = db.scalar(
            select(PayrollRun).order_by(PayrollRun.period_start.desc()).limit(1)
        )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "attendance_review": attendance_review,
        "attendance_approved": attendance_approved,
        "leaves": {str(k): int(v) for k, v in leaves_by_status.items()},
        "advances": {str(k): int(v) for k, v in advances_by_status.items()},
        "staff_actions": staff_actions,
        "hires": hires,
        "latest_payroll": (
            {"period": latest_run.period_start.isoformat(), "status": latest_run.status}
            if latest_run else None
        ),
    }


_PAYROLL_STATUS = {
    "draft": ("مسودة", "draft", "草稿"),
    "reopened": ("معاد فتحه", "reopened", "已重新打开"),
    "approved": ("معتمد", "approved", "已批准"),
    "closed": ("مغلق", "closed", "已关闭"),
}


def _payroll_status_label(status: str, language: str) -> str:
    labels = _PAYROLL_STATUS.get(status)
    if labels is None:
        return status
    return {"ar": labels[0], "en": labels[1], "zh": labels[2]}.get(language, labels[0])


def format_weekly_summary(report: dict[str, Any], language: str = "ar") -> str:
    """Trilingual presentation of the report dict; stored data untouched."""
    def m(ar: str, en: str, zh: str) -> str:
        return {"ar": ar, "en": en, "zh": zh}.get(language, ar)

    leaves = report["leaves"]
    advances = report["advances"]
    link = {"ar": "فتح السجلات", "en": "Open the records", "zh": "打开记录"}.get(language, "فتح السجلات")
    lines = [
        m(
            f"ملخص الأسبوع من {report['start']} إلى {report['end']} (قراءة فقط).",
            f"Weekly summary from {report['start']} to {report['end']} (read-only).",
            f"{report['start']} 至 {report['end']} 的周度总结（只读）。",
        ),
        m(
            f"الحضور: {report['attendance_review']} سجلًا ينتظر المراجعة، {report['attendance_approved']} سجلًا معتمدًا.",
            f"Attendance: {report['attendance_review']} records awaiting review, {report['attendance_approved']} approved.",
            f"考勤：{report['attendance_review']} 条待审核，{report['attendance_approved']} 条已批准。",
        ),
        f"{link}: /attendance/review",
        m(
            f"الإجازات: {leaves.get('draft', 0)} مسودة، {leaves.get('confirmed', 0)} معتمدة، {leaves.get('cancelled', 0)} ملغاة خلال الفترة.",
            f"Leave: {leaves.get('draft', 0)} draft, {leaves.get('confirmed', 0)} approved, {leaves.get('cancelled', 0)} cancelled in the window.",
            f"请假：窗口内 {leaves.get('draft', 0)} 条草稿，{leaves.get('confirmed', 0)} 条已批准，{leaves.get('cancelled', 0)} 条已取消。",
        ),
        f"{link}: /leave",
        m(
            f"السلف: {advances.get('pending', 0)} قيد الانتظار، {advances.get('approved', 0)} معتمدة، {advances.get('rejected', 0)} مرفوضة، {advances.get('paid', 0)} مدفوعة.",
            f"Advances: {advances.get('pending', 0)} pending, {advances.get('approved', 0)} approved, {advances.get('rejected', 0)} rejected, {advances.get('paid', 0)} paid.",
            f"预支：{advances.get('pending', 0)} 笔待处理，{advances.get('approved', 0)} 笔已批准，{advances.get('rejected', 0)} 笔已拒绝，{advances.get('paid', 0)} 笔已支付。",
        ),
        f"{link}: /advances",
        m(
            f"إجراءات الموظفين المسجلة: {report['staff_actions']} — تعيينات جديدة: {report['hires']}.",
            f"Staff actions recorded: {report['staff_actions']} — new hires: {report['hires']}.",
            f"已登记员工处分/措施：{report['staff_actions']} 条——新入职：{report['hires']} 人。",
        ),
    ]
    lines.append(f"{link}: /staff-actions")
    activity_zero = (
        report["attendance_review"] == 0
        and report["attendance_approved"] == 0
        and not any(report["leaves"].values())
        and not any(report["advances"].values())
        and report["staff_actions"] == 0
        and report["hires"] == 0
    )
    if activity_zero:
        lines.append(m(
            "ملاحظة: الأرقام أعلاه تغطي السجلات المسجلة في النظام فقط (الحضور، الإجازات، السلف، إجراءات الموظفين، الرواتب)، والصفر يعني عدم وجود سجلات في هذه المصادر — لا يُستنتج منه أنه لم يحدث عمل فعليًا.",
            "Note: the figures above cover only sources recorded in the system (attendance, leaves, advances, staff actions, payroll). A zero means no records in those sources; it does not show that no work actually happened.",
            "说明：以上数字仅涵盖系统中已记录的来源（考勤、请假、预支、员工操作、工资）。零表示这些来源中没有记录，并不代表实际没有发生工作。",
        ))

    latest = report["latest_payroll"]
    if latest:
        status = _payroll_status_label(latest["status"], language)
        lines.append(m(
            f"أحدث مسير رواتب: {latest['period']} — الحالة: {status}.",
            f"Latest payroll run: {latest['period']} — status: {status}.",
            f"最近工资批次：{latest['period']}——状态：{status}。",
        ))
        lines.append(f"{link}: /payroll")
    else:
        lines.append(m(
            "لا يوجد مسير رواتب منشأ بعد.",
            "No payroll run has been created yet.",
            "尚未创建工资批次。",
        ))
    return "\n".join(lines)
