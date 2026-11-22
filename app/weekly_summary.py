"""On-demand weekly work summary: read-only counts over a 7-day window.

Pure reads, deterministic counts, no business rules of its own. The report
is advisory: it tells the HR admin what happened and what is waiting, it
never changes or recommends changes to data.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

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
        "本周总结", "每周总结", "本周摘要", "周度总结",
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
        staff_actions = count(select(func.count(StaffAction.id)).where(
            func.date(StaffAction.created_at).between(start, end),
        ))
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


def format_weekly_summary(report: dict[str, Any], language: str = "ar") -> str:
    """Trilingual presentation of the report dict; stored data untouched."""
    def m(ar: str, en: str, zh: str) -> str:
        return {"ar": ar, "en": en, "zh": zh}.get(language, ar)

    leaves = report["leaves"]
    advances = report["advances"]
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
        m(
            f"الإجازات: {leaves.get('draft', 0)} مسودة، {leaves.get('approved', 0)} معتمدة، {leaves.get('cancelled', 0)} ملغاة خلال الفترة.",
            f"Leave: {leaves.get('draft', 0)} draft, {leaves.get('approved', 0)} approved, {leaves.get('cancelled', 0)} cancelled in the window.",
            f"请假：窗口内 {leaves.get('draft', 0)} 条草稿，{leaves.get('approved', 0)} 条已批准，{leaves.get('cancelled', 0)} 条已取消。",
        ),
        m(
            f"السلف: {advances.get('pending', 0)} قيد الانتظار، {advances.get('approved', 0)} معتمدة، {advances.get('rejected', 0)} مرفوضة، {advances.get('paid', 0)} مدفوعة.",
            f"Advances: {advances.get('pending', 0)} pending, {advances.get('approved', 0)} approved, {advances.get('rejected', 0)} rejected, {advances.get('paid', 0)} paid.",
            f"预支：{advances.get('pending', 0)} 笔待处理，{advances.get('approved', 0)} 笔已批准，{advances.get('rejected', 0)} 笔已拒绝，{advances.get('paid', 0)} 笔已支付。",
        ),
        m(
            f"إجراءات الموظفين المسجلة: {report['staff_actions']} — تعيينات جديدة: {report['hires']}.",
            f"Staff actions recorded: {report['staff_actions']} — new hires: {report['hires']}.",
            f"已登记员工处分/措施：{report['staff_actions']} 条——新入职：{report['hires']} 人。",
        ),
    ]
    latest = report["latest_payroll"]
    if latest:
        lines.append(m(
            f"أحدث مسير رواتب: {latest['period']} — الحالة: {latest['status']}.",
            f"Latest payroll run: {latest['period']} — status: {latest['status']}.",
            f"最近工资批次：{latest['period']}——状态：{latest['status']}。",
        ))
    else:
        lines.append(m(
            "لا يوجد مسير رواتب منشأ بعد.",
            "No payroll run has been created yet.",
            "尚未创建工资批次。",
        ))
    return "\n".join(lines)
