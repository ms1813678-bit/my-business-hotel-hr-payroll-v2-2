"""Deterministic smart scan for the AI assistant page (read-only).

Each check queries one operational queue and reports one of three states:
- "attention": items need a human look (count > 0);
- "clear": the queue is empty RIGHT NOW (worded as "nothing pending", never
  as "no problems");
- "unavailable": the check itself could not run, reported as unverified —
  an unavailable check is never presented as a clean queue.
"""
from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal
from app.models import (
    AdvanceRequest,
    AttendanceDailyRecord,
    AttendanceImportIssue,
    AttendanceMonth,
    LeaveRecord,
    PayrollRun,
    StaffAction,
)


def _count(statement) -> int:
    with SessionLocal() as db:
        return db.scalar(statement) or 0


_CHECKS: list[dict[str, Any]] = [
    {
        "key": "attendance_review",
        "url": "/attendance/review",
        "label": ("حضور يحتاج مراجعة", "Attendance needing review", "需要审核的考勤"),
        "count": lambda: _count(
            select(func.count(AttendanceDailyRecord.id)).where(
                AttendanceDailyRecord.status.in_(["needs_review", "ready_for_review"])
            )
        ),
    },
    {
        "key": "leave_drafts",
        "url": "/leave",
        "label": ("مسودات إجازة بانتظار المراجعة", "Leave drafts awaiting review", "待审核的请假草稿"),
        "count": lambda: _count(
            select(func.count(LeaveRecord.id)).where(LeaveRecord.status == "draft")
        ),
    },
    {
        "key": "advance_pending",
        "url": "/advances",
        "label": ("طلبات سلفة معلّقة أو معتمدة", "Advance requests pending or approved", "待处理或已批准的预支申请"),
        "count": lambda: _count(
            select(func.count(AdvanceRequest.id)).where(
                AdvanceRequest.status.in_(["pending", "approved"])
            )
        ),
    },
    {
        "key": "staff_action_drafts",
        "url": "/staff-actions",
        "label": ("مسودات جزاءات ومكافآت بانتظار الاعتماد", "Draft staff actions awaiting approval", "待审批的奖惩草稿"),
        "count": lambda: _count(
            select(func.count(StaffAction.id)).where(StaffAction.status == "draft")
        ),
    },
    {
        "key": "attendance_months_review",
        "url": "/attendance/months",
        "label": ("شهور حضور بانتظار الاعتماد", "Attendance months awaiting approval", "待批准的考勤月份"),
        "count": lambda: _count(
            select(func.count(AttendanceMonth.id)).where(AttendanceMonth.status == "under_review")
        ),
    },
    {
        "key": "import_issues",
        "url": "/attendance/import",
        "label": ("مشاكل استيراد حضور بانتظار الحل", "Attendance import issues awaiting resolution", "待解决的考勤导入问题"),
        "count": lambda: _count(
            select(func.count(AttendanceImportIssue.id)).where(
                AttendanceImportIssue.resolved_at.is_(None)
            )
        ),
    },
    {
        "key": "attendance_review_notes",
        "url": "/attendance/review",
        "label": ("سجلات حضور عليها ملاحظات مراجعة", "Attendance records carrying review notes", "带有审核备注的考勤记录"),
        "count": lambda: _count(
            select(func.count(AttendanceDailyRecord.id)).where(
                AttendanceDailyRecord.review_notes.isnot(None),
                AttendanceDailyRecord.review_notes != "",
                AttendanceDailyRecord.status.in_(["needs_review", "ready_for_review"]),
            )
        ),
    },
    {
        "key": "payroll_drafts",
        "url": "/payroll",
        "label": ("مسيرات رواتب غير معتمدة", "Unapproved payroll runs", "未批准的工资批次"),
        "count": lambda: _count(
            select(func.count(PayrollRun.id)).where(PayrollRun.status.in_(["draft", "reopened"]))
        ),
    },
]


def build_smart_scan(
    language: str = "ar",
    count_fn: Callable[[dict[str, Any]], int] | None = None,
) -> list[dict[str, Any]]:
    lang_index = {"ar": 0, "en": 1, "zh": 2}.get(language, 0)
    results = []
    for check in _CHECKS:
        entry = {"key": check["key"], "url": check["url"], "label": check["label"][lang_index]}
        try:
            n = count_fn(check) if count_fn else check["count"]()
        except (SQLAlchemyError, RuntimeError):
            entry["status"] = "unavailable"
            entry["count"] = None
        else:
            entry["status"] = "attention" if n > 0 else "clear"
            entry["count"] = n
        results.append(entry)
    return results
