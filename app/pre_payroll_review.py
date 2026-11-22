"""Deterministic pre-payroll review (read-only).

Approved scope: before a payroll run is approved, point the HR administrator
at (a) attendance that is not settled for the month and (b) deductions that
look unusual against the employee's own payroll history, each with the
supporting records. These are review suggestions, never verdicts: the report
changes nothing, approves nothing, and an employee without enough history is
reported as "insufficient data", never as "no issues".

Deterministic criteria (no model guessing):
- Unsettled attendance: approved payroll item employee has attendance daily
  records in the month whose status is needs_review or ready_for_review.
- Unusual deduction: the item's reviewable deductions (absence + lateness +
  staff sanctions + other deductions; scheduled advance installments are
  expected repayments and excluded) are at least twice the employee's average
  over their previous payroll items (up to 3 earlier runs) AND at least 100
  EGP above that average. With no previous items the baseline is reported as
  insufficient data.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AttendanceDailyRecord, PayrollItem, PayrollRun

_UNSETTLED_ATTENDANCE = ("needs_review", "ready_for_review")
_BASELINE_RUNS = 3
_RATIO_THRESHOLD = Decimal("2")
_ABS_FLOOR = Decimal("100")


def _month_bounds(month: date) -> tuple[datetime, datetime]:
    start = datetime(month.year, month.month, 1)
    if month.month == 12:
        end = datetime(month.year + 1, 1, 1)
    else:
        end = datetime(month.year, month.month + 1, 1)
    return start, end


def _reviewable_deductions(item: PayrollItem) -> Decimal:
    return (
        Decimal(item.absence_deduction)
        + Decimal(item.lateness_deduction)
        + Decimal(item.staff_sanction_amount)
        + Decimal(item.other_deductions)
    )


def build_pre_payroll_review(month: date) -> dict[str, Any]:
    with SessionLocal() as db:
        run = db.scalar(
            select(PayrollRun).where(PayrollRun.period_start == month).order_by(PayrollRun.id.desc())
        )
        report: dict[str, Any] = {
            "month": month.isoformat(),
            "run": None,
            "unsettled_attendance": [],
            "unusual_deductions": [],
            "insufficient_baseline": [],
            "note": "note_readonly_pre_payroll",
        }
        if run is None:
            report["run"] = {"status": "not_created"}
            return report
        report["run"] = {"id": run.id, "status": run.status}

        items = db.scalars(
            select(PayrollItem).where(PayrollItem.payroll_run_id == run.id)
        ).all()
        start, end = _month_bounds(month)
        for item in items:
            unsettled = db.scalars(
                select(AttendanceDailyRecord)
                .where(
                    AttendanceDailyRecord.employee_id == item.employee_id,
                    AttendanceDailyRecord.work_date >= start.date(),
                    AttendanceDailyRecord.work_date < end.date(),
                    AttendanceDailyRecord.status.in_(_UNSETTLED_ATTENDANCE),
                )
                .order_by(AttendanceDailyRecord.work_date)
            ).all()
            if unsettled:
                report["unsettled_attendance"].append({
                    "employee_code": item.employee_code_snapshot,
                    "employee_name": item.employee_name_snapshot,
                    "records": [
                        {"id": record.id, "work_date": record.work_date.isoformat(), "status": record.status}
                        for record in unsettled
                    ],
                })

            current = _reviewable_deductions(item)
            previous_runs = db.scalars(
                select(PayrollRun)
                .where(PayrollRun.period_start < month, PayrollRun.id != run.id)
                .order_by(PayrollRun.period_start.desc())
                .limit(_BASELINE_RUNS)
            ).all()
            previous_items = []
            for previous_run in previous_runs:
                previous_item = db.scalar(
                    select(PayrollItem).where(
                        PayrollItem.payroll_run_id == previous_run.id,
                        PayrollItem.employee_id == item.employee_id,
                    )
                )
                if previous_item is not None:
                    previous_items.append((previous_run.period_start, previous_item))
            if not previous_items:
                if current > 0:
                    report["insufficient_baseline"].append({
                        "employee_code": item.employee_code_snapshot,
                        "employee_name": item.employee_name_snapshot,
                        "current_deductions": str(current),
                        "item_id": item.id,
                    })
                continue
            baseline = sum(_reviewable_deductions(prev) for _, prev in previous_items) / len(previous_items)
            if (
                current > 0
                and current >= baseline * _RATIO_THRESHOLD
                and current - baseline >= _ABS_FLOOR
            ):
                report["unusual_deductions"].append({
                    "employee_code": item.employee_code_snapshot,
                    "employee_name": item.employee_name_snapshot,
                    "current_deductions": str(current),
                    "baseline_average": str(baseline.quantize(Decimal("0.01"))),
                    "baseline_months": [m.isoformat() for m, _ in previous_items],
                    "item_id": item.id,
                })
        return report


def _msg(language: str, ar: str, en: str, zh: str) -> str:
    return {"ar": ar, "en": en, "zh": zh}.get(language, ar)


def format_pre_payroll_review(report: dict[str, Any], language: str = "ar") -> str:
    month = report["month"]
    lines = [
        _msg(language,
             f"مراجعة ما قبل اعتماد رواتب {month} — اقتراحات مراجعة فقط، لا تغيّر ولا تعتمد شيئًا.",
             f"Pre-payroll review for {month} — review suggestions only; nothing is changed or approved.",
             f"{month} 工资批准前检查 — 仅供参考建议，不会更改或批准任何内容。")
    ]
    run = report.get("run") or {}
    if run.get("status") == "not_created":
        lines.append(_msg(language,
            "لا يوجد مسير رواتب لهذا الشهر بعد؛ أنشئ المسير أولًا ثم أعد المراجعة قبل الاعتماد.",
            "No payroll run exists for this month yet; create the run first, then re-run this review before approving.",
            "该月份尚未创建工资批次；请先创建批次，然后在批准前重新检查。"))
        return "\n".join(lines)
    lines.append(_msg(language,
        f"حالة المسير: {run.get('status')}.",
        f"Run status: {run.get('status')}.",
        f"批次状态：{run.get('status')}。"))

    unsettled = report["unsettled_attendance"]
    if unsettled:
        lines.append(_msg(language,
            f"حضور غير محسوم يحتاج تسوية قبل الاعتماد ({len(unsettled)} موظف):",
            f"Unsettled attendance to resolve before approval ({len(unsettled)} employees):",
            f"批准前需要处理的未结考勤（{len(unsettled)} 名员工）："))
        for entry in unsettled:
            days = ", ".join(f"{r['work_date']} (#{r['id']}, {r['status']})" for r in entry["records"])
            lines.append(f"- {entry['employee_code']} — {entry['employee_name']}: {days}")
    else:
        lines.append(_msg(language,
            "لا يوجد حضور غير محسوم لموظفي هذا المسير.",
            "No unsettled attendance for the employees in this run.",
            "该批次员工没有未结考勤。"))

    unusual = report["unusual_deductions"]
    if unusual:
        lines.append(_msg(language,
            f"خصومات غير معتادة مقارنة بتاريخ الموظف ({len(unusual)}):",
            f"Deductions unusual against the employee's own history ({len(unusual)}):",
            f"与员工本人历史相比异常扣款（{len(unusual)}）："))
        for entry in unusual:
            lines.append(_msg(language,
                f"- {entry['employee_code']} — {entry['employee_name']}: {entry['current_deductions']} جنيه مقابل متوسط سابق {entry['baseline_average']} (أشهر المقارنة: {', '.join(entry['baseline_months'])}) — راجع بند الرواتب #{entry['item_id']}.",
                f"- {entry['employee_code']} — {entry['employee_name']}: {entry['current_deductions']} EGP vs previous average {entry['baseline_average']} (baseline months: {', '.join(entry['baseline_months'])}) — review payroll item #{entry['item_id']}.",
                f"- {entry['employee_code']} — {entry['employee_name']}：{entry['current_deductions']} 埃镑，此前平均 {entry['baseline_average']}（对比月份：{', '.join(entry['baseline_months'])}）— 请查看工资条目 #{entry['item_id']}。"))
    else:
        lines.append(_msg(language,
            "لا توجد خصومات غير معتادة وفق معيار المقارنة المعلن (ضعف متوسط الموظف وفارق 100 جنيه على الأقل).",
            "No unusual deductions under the stated criterion (at least double the employee's average and at least 100 EGP above it).",
            "按照公示标准（至少为员工平均值的两倍且至少高出 100 埃镑），未发现异常扣款。"))

    insufficient = report["insufficient_baseline"]
    if insufficient:
        lines.append(_msg(language,
            f"بيانات تاريخية غير كافية للمقارنة ({len(insufficient)}) — هذا ليس تأكيدًا بعدم وجود مشاكل:",
            f"Insufficient history to compare ({len(insufficient)}) — this is not a confirmation that there are no issues:",
            f"历史数据不足，无法比较（{len(insufficient)}）— 这不代表没有问题："))
        for entry in insufficient:
            lines.append(f"- {entry['employee_code']} — {entry['employee_name']}: {entry['current_deductions']} (#{entry['item_id']})")

    lines.append(_msg(language,
        "المصدر: مسير الرواتب وبنوده وسجلات الحضور اليومية المحلية. القرار النهائي لمسؤول الموارد البشرية.",
        "Source: the local payroll run, its items, and daily attendance records. The final decision belongs to the HR administrator.",
        "来源：本地工资批次、工资条目和每日考勤记录。最终决定权在人力资源管理员。"))
    return "\n".join(lines)
